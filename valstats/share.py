"""Pooling finished matches with other installs of this program.

Everything this program knows, it worked out from matches it downloaded one at
a time. That is why a cold cache is useless and a warm one is good: party
detection needs to have seen a pair before, and the 0-1000 score needs a
population to rank against. Both of those get better with other people's
matches in the same way they get better with your own - and a match is a fact
that never changes, so two installs that saw the same match agree about it.

That is the whole idea: an install that opts in posts the finished matches it
parsed, and gets back a pool built from everyone else's.

What leaves the machine
-----------------------
One record per finished match, assembled by Encounters.bundle and nowhere else:

    the match      id, map, queue, exact start time, length, final score
    each of ten    side, party, agent, and the scoreboard line - score, rounds,
                   kills, deaths, assists, shot placement, KAST, damage

What does not leave the machine, ever:

    Riot IDs       no name, no tag, from any table. encounters is never read.
    your tokens    the lockfile, the access token, the entitlement, your keys
    rank history   rank_snapshots stays local; a pooled match carries only what
                   the match record itself said
    who you are    no account name, no machine name, nothing derived from
                   either - see the install id below, which is the one handle
                   that does go, and is random

Identifiers are hashed
----------------------
A puuid is not a name, but it is a durable handle on a real person, and the
pool is a public file that anybody can download. So every puuid, party id and
match id is replaced by HMAC-SHA256 truncated to 16 bytes, under a salt that
ships with the program. Two installs hash the same player to the same string,
which is all the pool needs; nobody who downloads the file can walk it back to
a Riot ID through the leaderboard, which is what would otherwise happen.

Be clear about what that is worth. The salt ships in the client, so it is not a
secret from someone who bothers. It stops the pool being a *scrapeable roster*
of accounts - which is the realistic risk - but it does not stop somebody who
already has a list of puuids from checking whether a particular one is in
there. If that matters for your pool, change share_salt in config.json and the
pool becomes private to the people you give the new salt to.

The install id
--------------
One push also carries a random 16-hex handle for this install, made once by
install_id() and kept in the database. It is not derived from the machine, the
account or the puuid - it is 8 bytes from the system random source.

It is there because the pool has to be able to tell "two people sent this
match" from "one person sent it twice", and that distinction is the whole of
what makes a pooled match believable: a match nobody else ever saw is one
person's word, and the pool marks it as such. The cost is that one install's
pushes are linkable to each other; they are linkable to nothing else, and
`share reset-id` throws the handle away for a new one.

Off unless asked
----------------
share_stats is false in the shipped config and the program asks once, in
writing, before the first upload. Nothing here runs during a live lobby: the
queue is drained between matches, so uploading never competes with the table.
"""

import hashlib
import hmac
import json
import time
from datetime import datetime, timezone

import requests

# The default salt. Public by construction - see the docstring. Changing it in
# config.json forks the pool: a different salt hashes the same player to a
# different string, so the two populations simply never meet.
DEFAULT_SALT = "valstats-public-pool-v1"

# Wire format. Bump when the meaning of a field changes, so a relay serving an
# older pool can turn away records it would otherwise merge wrongly.
WIRE_VERSION = 1

# Matches per request. Ten lines each, so 50 is roughly 120 KB of JSON - well
# inside what a Worker will take, and few enough that a failure costs little.
BATCH = 50

# Give up on one push after this long. The queue survives, so a timeout costs
# nothing but the attempt.
TIMEOUT = 20.0

# Fields of a scoreboard line that go out, and the short names they go out
# under. Short because the pool is a text file that gets downloaded a lot.
LINE_FIELDS = (
    ("score", "sc"),
    ("rounds", "r"),
    ("kills", "k"),
    ("deaths", "d"),
    ("assists", "a"),
    ("hs", "hs"),
    ("bs", "bs"),
    ("ls", "ls"),
    ("kast", "kast"),
    ("dmg_dealt", "dd"),
    ("dmg_received", "dr"),
    ("won", "w"),
)

CONSENT = """
  Sharing finished matches with the public pool
  ---------------------------------------------
  This install can post the matches it has already downloaded, and use the
  matches other installs posted. More matches means party detection that has
  seen a pair before, and a fairer 0-1000 score.

  What would be sent, per finished match:
      map, queue, exact start time, length, final score,
      and for all ten players: side, party, agent and the scoreboard line.

  What is never sent:
      Riot IDs, your tokens or API keys, your rank history, anything
      identifying you or your machine.

  Every puuid, party and match id is hashed before it leaves this machine.
  The pool is a public file. Read valstats/share.py before saying yes.

  This is off unless you turn it on. Set "share_stats": true in config.json.
"""


class Blocked(Exception):
    """The relay turned the push away and retrying now will not help."""


def token(value, salt):
    """One identifier as it appears in the pool: HMAC-SHA256, first 16 bytes.

    Empty in, empty out - a line with no side or no party keeps saying so
    rather than acquiring a hash of the empty string, which would read as a
    real party that every such line belonged to.
    """
    if not value:
        return ""
    digest = hmac.new(salt.encode("utf-8"), str(value).encode("utf-8"), hashlib.sha256)
    return digest.hexdigest()[:32]


def record(match_id, entry, salt):
    """One match as it goes on the wire, from one Encounters.bundle entry."""
    meta = entry.get("meta") or {}
    lines = []
    for line in entry.get("lines") or []:
        if not line.get("puuid"):
            continue
        out = {
            "p": token(line["puuid"], salt),
            "t": line.get("team") or "",
            "g": token(line.get("party"), salt),
            "c": line.get("agent") or "",
        }
        for field, short in LINE_FIELDS:
            out[short] = int(line.get(field) or 0)
        lines.append(out)
    lines.sort(key=lambda line: line["p"])
    return {
        "m": token(match_id, salt),
        "map": meta.get("map_id") or "",
        "q": meta.get("queue") or "",
        "t": meta.get("started_at") or "",
        "len": int(meta.get("length") or 0),
        "sc": meta.get("score") or "",
        "p": lines,
    }


def payload(db, match_ids, salt):
    """The body of one push: whole matches only, ten lines or it is not sent.

    A match that came back short - a player missing because their line was
    never parsed - is dropped rather than pooled. Half a scoreboard is the one
    thing a shared dataset cannot recover from, because nobody downstream can
    tell it from a match that really was played four-a-side.
    """
    bundle = db.bundle(match_ids)
    out = []
    for match_id, entry in bundle.items():
        built = record(match_id, entry, salt)
        if whole(built):
            out.append(built)
    out.sort(key=lambda item: item["t"])
    return out


def whole(built):
    """Is this record a whole match: ten players, five a side, and a time?

    The same question the relay asks on arrival. Asking it here too means a
    match that cannot be pooled is never counted as sent, so it comes back
    round after the next backfill instead of being lost to the ledger.
    """
    lines = built.get("p") or []
    if len(lines) != 10 or not built.get("t"):
        return False
    sides = {}
    for line in lines:
        sides[line["t"]] = sides.get(line["t"], 0) + 1
    return sorted(sides.values()) == [5, 5] and "" not in sides


def push(config, db, limit=None, quiet=True, say=print):
    """Send what has not gone yet. Returns how many matches were accepted.

    Never raises on a network problem: a pool that cannot be reached is not a
    reason to interrupt anything, and the queue is in the database, so the next
    run picks up exactly where this one stopped.
    """
    if not config.get("share_stats"):
        return 0
    endpoint = (config.get("share_endpoint") or "").strip()
    if not endpoint:
        if not quiet:
            say("[share] share_stats is on but share_endpoint is empty - nothing to send to")
        return 0

    salt = config.get("share_salt") or DEFAULT_SALT
    waiting = db.unshared(limit or BATCH)
    if not waiting:
        if not quiet:
            say("[share] nothing waiting")
        return 0

    body = payload(db, waiting, salt)
    if not body:
        return 0

    try:
        accepted = _post(endpoint, body, config, install_id(db))
    except Blocked as exc:
        if not quiet:
            say(f"[share] refused: {exc}")
        return 0
    except (requests.RequestException, ValueError) as exc:
        if not quiet:
            say(f"[share] could not reach the pool: {exc}")
        return 0

    # Only what actually went out. A match held back for being incomplete is
    # left unmarked on purpose, so it is offered again once a backfill has
    # filled in what it was missing.
    sent = {match_id for match_id in waiting if _went(db, match_id, body, salt)}
    db.mark_shared(sorted(sent))
    if not quiet:
        held = len(waiting) - len(sent)
        note = f", {held} held back as incomplete" if held else ""
        say(f"[share] sent {len(body)} matches, {accepted} were new to the pool{note}")
    return accepted


def _went(db, match_id, body, salt):
    """Was this local match among the records that were actually sent?"""
    hashed = token(match_id, salt)
    return any(record["m"] == hashed for record in body)


def _post(endpoint, body, config, src=""):
    """One POST, with the two retries a free relay occasionally needs."""
    blob = {"v": WIRE_VERSION, "salt": _salt_id(config), "matches": body}
    if src:
        blob["src"] = src
    last = None
    for attempt in range(3):
        response = requests.post(
            endpoint,
            json=blob,
            timeout=TIMEOUT,
            headers={"content-type": "application/json", "user-agent": "valstats-share/1"},
        )
        if response.status_code in (400, 401, 403, 409, 413, 422):
            raise Blocked(f"HTTP {response.status_code} {response.text[:200]}")
        if response.ok:
            try:
                return int((response.json() or {}).get("accepted", len(body)))
            except ValueError:
                return len(body)
        last = f"HTTP {response.status_code}"
        time.sleep(1.5 * (attempt + 1))
    raise requests.RequestException(last or "no answer")


def install_id(db):
    """A random handle for this install, made once and kept in the database.

    Not derived from anything: not the machine, not the account, not the puuid.
    A fresh 16 hex digits from the system random source, which is exactly as
    much as the pool needs to tell "two people sent this match" from "one
    person sent it twice" - the thing that decides whether a match is believed.

    It links one install's pushes to each other and to nothing else, and
    `share reset-id` throws it away for a new one.
    """
    import secrets

    got = db.setting("share_install_id")
    if not got:
        got = secrets.token_hex(8)
        db.remember("share_install_id", got)
    return got


def _salt_id(config):
    """Which pool this record belongs to, without revealing the salt itself.

    Two installs on different salts must not have their records merged: the
    same player hashes differently under each, so merging them would invent
    strangers. This says which pool a push belongs to and nothing else.
    """
    salt = config.get("share_salt") or DEFAULT_SALT
    return hashlib.sha256(f"pool:{salt}".encode("utf-8")).hexdigest()[:12]


def status(config, db, say=print):
    """What sharing is doing, for `python -m valstats share`."""
    counts = db.share_counts()
    if not config.get("share_stats"):
        say(CONSENT.rstrip())
        say("")
        say(f"  {counts['parsed']} finished matches are cached and would be offered.")
        return 0
    endpoint = (config.get("share_endpoint") or "").strip() or "(not set)"
    say(f"pool      {endpoint}")
    say(f"install   {install_id(db)}")
    say(f"salt      {_salt_id(config)}" + ("" if config.get("share_salt") else "  (default)"))
    say(f"shared    {counts['shared']} matches")
    say(f"waiting   {counts['waiting']} matches")
    return 0


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def main(argv=None):
    """`python -m valstats share [now|status|reset]`."""
    from .config import load as load_config
    from .db import Encounters

    argv = list(argv or ())
    command = (argv[0].lower() if argv else "status")
    config = load_config()
    db = Encounters(True)
    try:
        if command in ("status", ""):
            return status(config, db)
        if command == "now":
            if not config.get("share_stats"):
                print(CONSENT.rstrip())
                return 1
            sent = push(config, db, limit=BATCH, quiet=False)
            return 0 if sent >= 0 else 1
        if command == "reset":
            db.forget_shared()
            print("share ledger cleared - every cached match will be offered again")
            return 0
        if command in ("reset-id", "resetid"):
            db.remember("share_install_id", "")
            print("install id forgotten - the next push introduces itself as somebody new")
            return 0
        print(f"unknown share command {command!r} - try status, now or reset")
        return 2
    finally:
        if db.conn:
            db.conn.close()


if __name__ == "__main__":
    raise SystemExit(main(__import__("sys").argv[1:]))
