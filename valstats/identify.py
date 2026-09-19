"""Put Riot IDs to the puuids the local memory never managed to name.

    python -m valstats identify              everyone still nameless
    python -m valstats identify <name|puuid> one player, source by source
    python -m valstats identify leaderboard  re-dump the act leaderboard

The live table and the post-match scoreboard both name whoever they can as
they go. This is the same machinery run deliberately and out loud: it asks
every source in config.json's reveal_sources, says which one answered for
whom, and writes what it learns back into encounters.db so the next lobby
already knows.

Asked about one player it goes the other way round - every source is asked
about that single puuid, whether an earlier one already answered or not, and
the answers are printed side by side. That is the view worth having when the
question is not "who is this" but "where would I even find out".
"""

from datetime import datetime, timezone

import requests
from rich.markup import escape
from rich.table import Table

from . import identity, render
from .client import Client, ClientUnavailable
from .config import load as load_config
from .content import Content
from .db import Encounters

# How many puuids to hand a source at once. The name service takes a list and
# answers in one request; the rest do not care.
BATCH = 100

# A puuid is a uuid, and nothing else here looks like one.
PUUID_LENGTH = 36


def _when(ts):
    if not ts:
        return ""
    try:
        moment = datetime.fromisoformat(ts)
    except ValueError:
        return ts[:16]
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone().strftime("%Y-%m-%d %H:%M")


def _connect(config):
    """The client, or None with the reason already printed.

    Two of the seven sources - the local memory and, for a player you have met
    before, nothing else - work with the game shut. The rest need a session,
    so this is worth saying rather than failing seven times over.
    """
    try:
        return Client(config).connect()
    except ClientUnavailable as exc:
        render.warn(f"{exc}")
        render.warn(
            "only the local memory can answer with the client closed - "
            "start it and log in for the rest"
        )
        return None


def _resolver(config, client, db, content, order=None):
    return identity.Resolver.build(config, client=client, db=db, content=content, order=order)


def _content():
    try:
        return Content().load(want_skins=False)
    except Exception as exc:  # noqa: BLE001 - being offline is a state, not a fault
        render.warn(f"no cached game content ({exc}) - the leaderboard source is off")
        return None


def _enabled_note(resolver):
    """Say what is switched on, and why anything switched off is switched off."""
    render.console.print()
    table = Table(header_style="dim", box=None, pad_edge=False, padding=(0, 2))
    table.add_column("Source")
    table.add_column("")
    for source in resolver.sources:
        state = "[dim]off - " + escape(source.unavailable) + "[/dim]" if source.unavailable else "on"
        table.add_row(source.label, state)
    render.console.print(table)


# ------------------------------------------------------------------ one player


def _one(db, resolver, puuid, label=""):
    """Ask every source about a single puuid and print what each one said.

    Every source, not the first that answers: the point of this view is the
    disagreement. A name that only the local memory has is a name from months
    ago; one the name service and a leaderboard dump agree on is current.
    """
    render.console.print()
    render.console.print(f"[bold]{escape(label or puuid)}[/bold]")
    render.console.print(f"  [dim]puuid[/dim] {puuid}")

    table = Table(header_style="dim", box=None, pad_edge=False, padding=(0, 2))
    table.add_column("Source")
    table.add_column("Riot ID")
    table.add_column("As of", style="dim")

    answers = {}
    for source in resolver.sources:
        if source.unavailable:
            table.add_row(source.label, "[dim]-[/dim]", f"[dim]off: {escape(source.unavailable)}[/dim]")
            continue
        try:
            found = source.lookup([puuid], {"phase": "after", "details": None}) or {}
        except Exception as exc:  # noqa: BLE001 - one source failing is not the report failing
            table.add_row(source.label, "[dim]-[/dim]", f"[dim]{escape(str(exc)[:40])}[/dim]")
            continue
        named = found.get(puuid)
        if not named:
            table.add_row(source.label, "[dim]-[/dim]", "")
            continue
        answers[source.key] = named
        table.add_row(source.label, escape(named.name), _when(named.when) or "now")
    render.console.print()
    render.console.print(table)

    if answers:
        for named in answers.values():
            db.remember_found({puuid: named})
        distinct = {named.name for named in answers.values()}
        if len(distinct) > 1:
            render.warn(
                "the sources disagree - the one dated `now` is the Riot ID the "
                "account carries today, the rest are what it used to carry"
            )
    else:
        render.warn("nothing here can name that puuid")
        _leads(db, puuid)
    return 0


def _leads(db, puuid):
    """Who this player keeps turning up beside - the lead, when there is no name.

    Not an identification and not offered as one. A hidden account that plays
    every evening with the same visible person is findable through that person's
    match history, which is a thing you do by hand, on purpose, if you have a
    reason to.
    """
    mates = [row for row in db.teammates(puuid, limit=5) if row[2] > 1]
    if not mates:
        return
    render.console.print()
    render.console.print("[dim]seen on the same side as:[/dim]")
    for mate_puuid, name, shared in mates:
        who = escape(name) if name else f"{mate_puuid[:8]}... [dim](also unnamed)[/dim]"
        render.console.print(f"  {who} [dim]{shared} matches[/dim]")


# -------------------------------------------------------------------- everyone


def _everyone(db, resolver, limit):
    """Run the enabled sources over every puuid the memory could never name."""
    puuids = db.nameless(limit)
    if not puuids:
        render.info("every player in the local memory already has a Riot ID")
        return 0
    render.info(f"{len(puuids)} players in the local memory have never been named")

    found = {}
    for start in range(0, len(puuids), BATCH):
        chunk = puuids[start : start + BATCH]
        try:
            found.update(resolver.resolve(chunk, phase="after"))
        except (ClientUnavailable, requests.RequestException) as exc:
            render.warn(f"stopped early: {exc}")
            break
        render.info(f"  {min(start + BATCH, len(puuids))}/{len(puuids)} asked about...", repeat=True)

    if not found:
        render.warn("none of them could be named from any source that is switched on")
        return 0

    renamed = db.remember_found(found)
    counts = identity.tally(found)
    render.info(f"named {len(found)} of them: {identity.describe(counts, resolver.sources)}")
    if renamed != len(found):
        render.info(f"{renamed} of those were new to the local memory")

    table = Table(header_style="dim", box=None, pad_edge=False, padding=(0, 2))
    table.add_column("Riot ID")
    table.add_column("Source", style="dim")
    table.add_column("As of", style="dim")
    labels = {source.key: source.label for source in resolver.sources}
    for _puuid, named in sorted(found.items(), key=lambda item: item[1].name.lower()):
        table.add_row(escape(named.name), labels.get(named.source, named.source), _when(named.when))
    render.console.print()
    render.console.print(table)
    return 0


# ----------------------------------------------------------------- leaderboard


def _leaderboard(config, client, content):
    """Pull the act leaderboard now and say how much of it names anybody."""
    source = identity.Leaderboard(client, content, config.get("leaderboard_cache_hours", 12.0))
    if source.unavailable:
        render.warn(f"cannot read the leaderboard: {source.unavailable}")
        return 1
    render.info(f"dumping the act leaderboard for {client.region}...")

    def progress(named, total):
        render.info(f"  {named} named of {total or '?'}...", repeat=True)

    count = source.refresh(progress)
    if not count:
        render.warn("Riot would not serve the leaderboard")
        return 1
    render.info(f"{count} players on the board can be named by puuid - cached in {source.path.name}")
    return 0


# --------------------------------------------------------------------- command


def identify(query=None, limit=200):
    """The `identify` command: everyone, or one player, or the leaderboard."""
    config = load_config()
    db = Encounters()
    client = None
    try:
        content = _content()
        client = _connect(config)
        resolver = _resolver(config, client, db, content)

        if (query or "").strip().lower() in ("leaderboard", "board"):
            if client is None:
                return 1
            return _leaderboard(config, client, content)

        _enabled_note(resolver)
        if not resolver.usable("after"):
            render.warn(
                "no source is switched on - check reveal_sources in config.json"
            )
            return 1

        if not query:
            return _everyone(db, resolver, limit)

        puuid, label = _find(db, query)
        if not puuid:
            return 1
        return _one(db, resolver, puuid, label)
    finally:
        db.close()


def _find(db, query):
    """(puuid, label) for what was typed: a puuid as itself, a name looked up."""
    query = query.strip()
    if len(query) == PUUID_LENGTH and query.count("-") == 4:
        row = db.known_names([query])
        return query, (row[0][1] if row else "")

    from .lookup import suggest  # imported here: it pulls in the live completer

    found = suggest(db.named_players(), query)
    if not found:
        render.warn(
            f"nobody like {query!r} in the local memory - "
            "pass a puuid instead, or run it with no argument"
        )
        return None, ""
    if len(found) > 1 and (found[0]["name"] or "").lower() != query.lower():
        render.console.print()
        for row in found:
            render.console.print(f"  {escape(row['name'])} [dim]met {row['times']}x[/dim]")
        render.warn("more than one player looks like that - type more of the name")
        return None, ""
    return found[0]["puuid"], found[0]["name"]
