"""The other half of sharing: reading what everyone else pooled.

    python -m valstats pool           what has been downloaded, and from where
    python -m valstats pool sync      fetch it now
    python -m valstats pool forget    throw the downloaded copy away

Nothing here changes how the program works when the network is gone. The pool
is downloaded as one file, once in a while, into two tables of its own; every
lookup after that is a local query against SQLite, exactly like every lookup
before the pool existed. Pull the plug and the program keeps running on what it
has - which is the whole design of this thing and is not given up for this.

Why the pool lives in its own tables
------------------------------------
It would be less code to merge pooled rows into match_perf and let every
existing query pick them up for free. That is the wrong trade twice over.

The first reason is that it cannot work: the local tables are keyed by puuid
and the pool is keyed by HMAC(puuid), and there is no puuid to recover. The
join has to happen at the hash, which means the caller hashes what it knows and
asks the pool about that.

The second is that they are not the same kind of fact. A row in match_perf came
off a match-details payload this machine downloaded from Riot. A row in
pool_perf is a stranger's report of a match this machine never saw. Both are
probably true; only one is checked. Keeping them apart is what lets the table
say where a number came from, and lets local evidence win when they disagree.

What the pool is used for
-------------------------
  party detection   pooled matches carry Riot's own party id, so a pair can be
                    confirmed rather than inferred. See party.py.
  the 0-1000 score  a bigger population to rank against.
  form              lines for a player this machine has never met.

In each case the local answer is preferred where there is one, and the pooled
answer is marked as pooled where it is used.
"""

import json
from datetime import datetime, timezone

import requests

from . import render, share

# How long a downloaded pool is good for before a sync is worth making, in
# hours. The pool is rebuilt every twenty minutes at the far end, but nothing
# here needs to be that fresh: a match from this morning helps exactly as much
# tomorrow.
STALE_HOURS = 12.0

# Refuse a download bigger than this. A pool that has grown past it wants
# by-month files and a client that asks for the months it lacks, not one that
# quietly eats half a gigabyte because a URL said so.
MAX_BYTES = 64 * 1024 * 1024

TIMEOUT = 60.0


def urls(config):
    """(index url, data url) from the endpoint, or ("", "") if not set up.

    Derived from share_endpoint so there is one address to configure rather
    than three. The relay answers /pool with a redirect to wherever the file
    actually lives, which is what lets the repository move.
    """
    endpoint = (config.get("share_endpoint") or "").strip()
    if not endpoint:
        return "", ""
    base = endpoint.rsplit("/", 1)[0] if endpoint.endswith("/ingest") else endpoint.rstrip("/")
    want = "confirmed" if config.get("pool_trust", "confirmed") == "confirmed" else "latest"
    return f"{base}/pool?file=index.json", f"{base}/pool?file={want}.ndjson"


def parse(text):
    """An ndjson pool as a list of matches, skipping lines that will not parse."""
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict) and entry.get("m") and entry.get("p"):
            out.append(entry)
    return out


def due(db, hours=STALE_HOURS):
    """Is it time to fetch again? False when the last fetch is still fresh."""
    last = db.setting("pool_synced_at")
    if not last:
        return True
    try:
        when = datetime.fromisoformat(last)
    except ValueError:
        return True
    if not when.tzinfo:
        when = when.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - when).total_seconds() >= max(0.0, hours) * 3600


def sync(config, db, force=False, quiet=True, say=render.info):
    """Fetch the pool if it is due, and load it. Returns matches stored.

    Never raises on a network problem, for the same reason push does not: the
    copy already in the database is still good, and a pool that cannot be
    reached is not a reason to interrupt anything.
    """
    if not config.get("use_pool"):
        return 0
    index_url, data_url = urls(config)
    if not data_url:
        if not quiet:
            say("[pool] no share_endpoint set - nothing to download from")
        return 0
    if not force and not due(db, config.get("pool_refresh_hours", STALE_HOURS)):
        return 0

    try:
        # The index is a few hundred bytes and says what the data file will
        # hash to. If that matches what was loaded last time, the download is
        # skipped entirely - which is most syncs, most of the time.
        stamp = ""
        if index_url:
            index = requests.get(index_url, timeout=TIMEOUT).json()
            stamp = str(index.get("confirmed_sha256") or index.get("sha256") or "")
            if stamp and stamp == db.setting("pool_sha256") and not force:
                db.remember("pool_synced_at", share.now())
                return 0

        response = requests.get(data_url, timeout=TIMEOUT)
        response.raise_for_status()
        if len(response.content) > MAX_BYTES:
            if not quiet:
                say(f"[pool] refused a {len(response.content) / 1e6:.0f} MB download")
            return 0
        matches = parse(response.text)
    except (requests.RequestException, ValueError) as exc:
        if not quiet:
            say(f"[pool] could not fetch: {exc}")
        return 0

    if not matches:
        if not quiet:
            say("[pool] the pool is empty")
        db.remember("pool_synced_at", share.now())
        return 0

    stored = db.replace_pool(matches)
    db.remember("pool_synced_at", share.now())
    if stamp:
        db.remember("pool_sha256", stamp)
    if not quiet:
        counts = db.pool_counts()
        say(
            f"[pool] {stored} matches, {counts['players']} players, "
            f"{counts['first']} to {counts['last']}"
        )
    return stored


def hashes(config, puuids):
    """{puuid: hash} for a lobby, under whichever salt this install uses.

    The one place a local puuid meets the pool. Everything downstream works in
    hashes, which is why db.py never needs to know the salt exists.
    """
    salt = config.get("share_salt") or share.DEFAULT_SALT
    return {puuid: share.token(puuid, salt) for puuid in puuids if puuid}


def status(config, db, say=print):
    counts = db.pool_counts()
    index_url, data_url = urls(config)
    say(f"pool      {data_url or '(no share_endpoint set)'}")
    say(f"trust     {config.get('pool_trust', 'confirmed')}")
    say(f"enabled   {'yes' if config.get('use_pool') else 'no'}")
    say(f"synced    {db.setting('pool_synced_at') or 'never'}")
    say(f"matches   {counts['matches']}")
    say(f"players   {counts['players']}")
    if counts["matches"]:
        say(f"covering  {counts['first']} to {counts['last']}")
    return 0


def main(argv=None):
    """`python -m valstats pool [sync|forget]`."""
    from .config import load as load_config
    from .db import Encounters

    argv = list(argv or ())
    command = argv[0].lower() if argv else "status"
    config = load_config()
    db = Encounters(True)
    try:
        if command in ("status", ""):
            return status(config, db)
        if command == "sync":
            if not config.get("use_pool"):
                print('use_pool is false in config.json - nothing is downloaded')
                return 1
            sync(config, db, force=True, quiet=False, say=print)
            return status(config, db)
        if command == "forget":
            db.replace_pool([])
            db.remember("pool_sha256", "")
            print("downloaded pool cleared - local matches are untouched")
            return 0
        print(f"unknown pool command {command!r} - try status, sync or forget")
        return 2
    finally:
        if db.conn:
            db.conn.close()
