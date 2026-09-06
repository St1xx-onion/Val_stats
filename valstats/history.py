"""What the local memory knows, without a match running.

    python -m valstats who <name>     one player: when you met, how they play
    python -m valstats top [n]        the people you run into most
    python -m valstats backfill [n]   warm the cache from your own history
    python -m valstats calibration    what the 0-1000 score is measured against

None of these touch the network except `backfill`, which reads your own match
history and nothing else.
"""

from rich.markup import escape
from rich.table import Table

from . import render
from .client import Client, ClientUnavailable
from .config import load as load_config
from .content import Content
from .db import Encounters
from .perf import (
    BANDS,
    METRICS,
    MIN_POPULATION,
    MIN_POPULATION_ROUNDS,
    Calibration,
    aggregate,
    extract,
    load_calibration,
)

# Riot serves match history in pages; this is as many as it will give at once.
PAGE = 20

METRIC_LABELS = {"acs": "ACS", "kast": "KAST", "dd": "DD/round", "winrate": "Win%"}


def _name(entry):
    """Riot IDs can contain brackets, which rich would read as markup."""
    return escape(entry["name"] or "[hidden]")


def _short(timestamp):
    return (timestamp or "")[:10] or "?"


def _form_line(summary):
    if not summary:
        return "no cached matches yet"
    parts = [
        f"score {summary['rating']}",
        f"ACS {summary['acs']:.0f}" if summary["acs"] is not None else "ACS -",
        f"K/D {summary['kd']:.2f}",
    ]
    if summary["kast"] is not None:
        parts.append(f"KAST {summary['kast']:.0f}%")
    if summary["hs"] is not None:
        parts.append(f"HS {summary['hs']:.0f}%")
    parts.append(f"WR {summary['winrate']:.0f}%")
    parts.append(f"over {summary['matches']} cached matches ({summary['rounds']} rounds)")
    return "  ".join(parts)


def _rank_track(content, snapshots):
    """Their rank each time you met, collapsed to the moments it changed."""
    steps = []
    for snap in snapshots:
        label = content.tier(snap["tier"])["name"]
        if steps and steps[-1][0] == label:
            continue
        steps.append((label, _short(snap["ts"])))
    return " -> ".join(f"{label} ({when})" for label, when in steps)


# ------------------------------------------------------------------ commands


def who(query, limit=5):
    """Everything remembered about the players whose name matches `query`."""
    db = Encounters()
    try:
        found = db.find(query, limit)
        if not found:
            render.warn(f"nobody matching {query!r} in the local memory")
            return 1
        content = Content().load(want_skins=False)
        calibration = load_calibration(db, "auto")
        for entry in found:
            summary = aggregate(db.all_perf_rows(entry["puuid"]), calibration)
            render.console.print()
            render.console.print(
                f"[bold]{_name(entry)}[/bold]  "
                f"[dim]met {entry['times']}x, "
                f"{_short(entry['first_seen'])} - {_short(entry['last_seen'])}[/dim]"
            )
            render.console.print(f"  {_form_line(summary)}")
            track = _rank_track(content, db.snapshots(entry["puuid"]))
            if track:
                render.console.print(f"  [dim]rank when you met:[/dim] {track}")
        return 0
    finally:
        db.close()


def top(limit=20):
    """The people you keep running into, with whatever form is cached."""
    db = Encounters()
    try:
        people = db.most_met(limit)
        if not people:
            render.warn("no encounters recorded yet - play a match with this running")
            return 1
        calibration = load_calibration(db, "auto")
        table = Table(title=f"Most met - top {len(people)}", title_style="bold", header_style="dim")
        for header, justify in (
            ("Player", "left"),
            ("Met", "right"),
            ("First", "left"),
            ("Last", "left"),
            ("Score", "right"),
            ("ACS", "right"),
            ("K/D", "right"),
            ("N", "right"),
        ):
            table.add_column(header, justify=justify, no_wrap=True)
        for entry in people:
            summary = aggregate(db.all_perf_rows(entry["puuid"]), calibration)
            table.add_row(
                _name(entry),
                str(entry["times"]),
                _short(entry["first_seen"]),
                _short(entry["last_seen"]),
                str(summary["rating"]) if summary else "-",
                f"{summary['acs']:.0f}" if summary and summary["acs"] is not None else "-",
                f"{summary['kd']:.2f}" if summary else "-",
                str(summary["matches"]) if summary else "-",
            )
        render.console.print()
        render.console.print(table)
        totals = db.totals()
        render.info(
            f"local memory: {totals['players']} players, "
            f"{totals['matches']} matches parsed, {totals['lines']} player-lines"
        )
        return 0
    finally:
        db.close()


def backfill(count=20):
    """Parse your own recent matches so the first live lobby is not a cold start.

    Only your own history is read. Names are never taken from match-details, so
    this fills in the numbers, not the "seen before" counter.
    """
    config = load_config()
    db = Encounters()
    try:
        try:
            client = Client(config).connect()
        except ClientUnavailable as exc:
            render.error(f"{exc}")
            render.warn("Start the Riot Client and log in, then run this again.")
            return 1

        queue = config["performance_queue"]
        render.info(f"reading your last {count} {queue or 'ranked and unranked'} matches...")
        match_ids = []
        while len(match_ids) < count:
            page = client.match_history(
                client.puuid, min(PAGE, count - len(match_ids)), queue, start=len(match_ids)
            )
            found = [e.get("MatchID") for e in (page or {}).get("History") or [] if e.get("MatchID")]
            if not found:
                break
            match_ids.extend(found)

        if not match_ids:
            render.warn("no matches in your history for that queue")
            return 1

        fetched = skipped = 0
        for index, match_id in enumerate(match_ids, start=1):
            if db.is_parsed(match_id):
                skipped += 1
                continue
            try:
                details = client.match_details(match_id)
            except ClientUnavailable:
                details = client.match_details(match_id)
            if not details:
                continue
            parsed_id, per_player = extract(details)
            db.store_match_perf(parsed_id or match_id, per_player)
            fetched += 1
            render.info(f"  {index}/{len(match_ids)} matches...", repeat=True)

        totals = db.totals()
        render.info(
            f"done: {fetched} matches downloaded, {skipped} already cached; "
            f"{totals['lines']} player-lines in total"
        )
        return _calibration_note(db)
    finally:
        db.close()


def calibration():
    """What the 0-1000 score is currently measured against."""
    db = Encounters()
    try:
        population = db.population(MIN_POPULATION_ROUNDS)
        totals = db.totals()
        render.console.print()
        render.console.print(
            f"[bold]Score calibration[/bold]  [dim]{totals['lines']} player-lines cached, "
            f"{len(population)} players with at least {MIN_POPULATION_ROUNDS} rounds[/dim]"
        )
        active = load_calibration(db, "auto")
        if not active:
            render.warn(
                f"using the fixed bands - {MIN_POPULATION} qualifying players needed, "
                f"{len(population)} so far"
            )
        table = Table(header_style="dim")
        table.add_column("Metric")
        table.add_column("Fixed band 0 / 250", justify="right")
        table.add_column("Your population 25 / 50 / 75", justify="right")
        for key in METRICS:
            low, high = BANDS[key]
            spread = active.describe(key) if active else None
            table.add_row(
                METRIC_LABELS[key],
                f"{low:.0f} / {high:.0f}",
                " / ".join(f"{v:.0f}" for v in spread) if spread else "-",
            )
        render.console.print(table)
        if active:
            render.info(f"scoring against {active.players} players you have met")
        return 0
    finally:
        db.close()


def _calibration_note(db):
    population = db.population(MIN_POPULATION_ROUNDS)
    if len(population) >= MIN_POPULATION and Calibration.from_population(population):
        render.info(f"score now calibrated against {len(population)} players")
    else:
        render.info(
            f"{len(population)}/{MIN_POPULATION} players cached towards percentile scoring"
        )
    return 0
