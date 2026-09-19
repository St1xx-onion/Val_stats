"""What the local memory knows, without a match running.

    python -m valstats who <name>     one player: when you met, how they play
    python -m valstats mates <name>   who that player keeps queueing with
    python -m valstats match <id>     one shared match: the uuid behind M12
    python -m valstats top [n]        the people you run into most
    python -m valstats backfill [n]   warm the cache from your own history
    python -m valstats agents         your own agent pool, best first
    python -m valstats calibration    what the 0-1000 score is measured against

None of these touch the network except `backfill`, which reads your own match
history and nothing else.
"""

import requests
from rich.markup import escape
from rich.table import Table

from . import identity, mmr, party, render
from .client import Client, ClientUnavailable
from .config import load as load_config
from .content import Content
from .db import Encounters
from .perf import (
    BANDS,
    METRICS,
    by_agent,
    MIN_POPULATION,
    MIN_POPULATION_ROUNDS,
    Calibration,
    aggregate,
    extract,
    load_calibration,
    meta,
    names,
    tiers,
)

# Riot serves match history in pages; this is as many as it will give at once.
PAGE = 20

# How many puuids to put in one name-service request.
NAME_BATCH = 100

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


def _self_puuid(db, quiet=True):
    """Your own puuid, remembered once so these commands stay offline.

    The Riot client is the only thing that can tell us, so the first run that
    finds it running writes it down. After that nothing here needs the game.
    """
    known = db.setting("self_puuid")
    if known:
        return known
    try:
        found = Client(load_config()).connect().puuid
    except ClientUnavailable:
        return None
    db.remember_own(found)
    return found


def _met_in(db, puuid, times):
    """The matches you shared with one player: date and local match number.

    Encounters logged before matches were linked survive only as a count, so
    this says how much of `times` it can actually name.
    """
    rows = db.met_in(puuid)
    if not rows:
        return
    listed = [f"{_label(row['num'])} {_short(row['ts'])}" for row in rows]
    known = f"{len(rows)} of {times}" if times and len(rows) < times else str(len(rows))
    render.console.print(f"  [dim]met in ({known} matches):[/dim] {'  '.join(listed)}")


def _label(num):
    return f"M{num}" if num else "M?"


def _parse_label(text):
    """Accept M12, m12 or 12 - all the same match."""
    try:
        return int(str(text).strip().lstrip("Mm#").strip())
    except ValueError:
        return None


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
        _link(db)
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
            _named_by(db, entry["puuid"])
            _met_in(db, entry["puuid"], entry["times"])
        render.console.print()
        render.info("python -m valstats match M12 - the Riot match id behind a number")
        return 0
    finally:
        db.close()


def _named_by(db, puuid):
    """Where this Riot ID came from, when it did not come from the lobby.

    Worth a line only when something other than the plain name service put it
    there - if a player has always been visible, saying so every time is noise.
    Two sources disagreeing is the interesting case: an account that has been
    renamed reads as the leaderboard and the name service saying different
    things, and this is where you see that.
    """
    rows = [row for row in db.name_provenance(puuid) if row[0] != "name-service"]
    if not rows:
        return
    parts = [f"{name} [dim]({source}, {_short(ts)})[/dim]" for source, name, ts in rows]
    render.console.print(f"  [dim]named by:[/dim] {'  '.join(parts)}")


def _link(db):
    """Fold matches of your own that are already cached into the encounter log.

    Free and idempotent: it is one INSERT over data the cache already holds, so
    running it on every lookup just picks up whatever was played since.
    """
    added = db.link_own_matches(_self_puuid(db))
    db.label_missing()
    if added:
        render.info(f"recovered {added} encounters from matches already cached")


def match(label):
    """One numbered match: the Riot id to search for, and who was in it."""
    num = _parse_label(label)
    if num is None:
        render.warn(f"{label!r} is not a match number - they look like M12")
        return 2
    db = Encounters()
    try:
        _link(db)
        found = db.match_by_label(num)
        if not found:
            render.warn(f"no match numbered {_label(num)} in the local memory")
            return 1
        content = Content().load(want_skins=False)
        where = content.map_name(found["map_id"] or "") or "?"
        render.console.print()
        render.console.print(
            f"[bold]{_label(found['num'])}[/bold]  {_short(found['ts'])}  "
            f"[dim]{where}  {found['queue'] or '?'}[/dim]"
        )
        render.console.print(f"  [dim]match id[/dim] {found['match_id']}")
        people = db.roster(found["match_id"])
        if people:
            listed = "  ".join(f"{_name(row)} [dim]({row['times'] or 1}x)[/dim]" for row in people)
            render.console.print(f"  [dim]met here:[/dim] {listed}")
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


def mates(query, limit=15):
    """Who one player keeps queueing with, as far as the local cache can tell.

    This is the closest thing to a friends list that exists. Riot publishes
    nobody's: the only friends endpoint is the Riot Client's own, it answers
    about the account that is logged in, and there is no request - official,
    local or third-party - that hands you somebody else's list. What can be
    answered is the question underneath it, which is usually the one actually
    being asked: who does this person play with?

    So it reads the same evidence party.py gives a live lobby, pointed at one
    player instead of at ten. Same-side matches only, and split into separate
    occasions, because a pair that came back on another evening means something
    that a pair who happened to share one long night does not.

    Nothing here goes out to the network, and it only sees matches that are
    already cached - so it answers about the part of their history this machine
    happens to have, never about all of it.
    """
    db = Encounters()
    try:
        found = db.find(query, limit=5)
        if not found:
            render.warn(f"nobody matching {query!r} in the local memory")
            render.info("python -m valstats top - the names the memory does have")
            return 1
        _link(db)
        entry = found[0]
        if len(found) > 1:
            also = ", ".join(_name(other) for other in found[1:])
            render.info(f"{query!r} also matches {also} - showing the one met most")

        gap = load_config()["party_session_gap_hours"]
        bonds = {
            other: party.weigh(times, gap)
            for other, times in db.mates_of(entry["puuid"]).items()
        }
        if not bonds:
            render.warn(f"no cached match of {_name(entry)} has a side recorded yet")
            render.info("python -m valstats backfill - parse more matches into the cache")
            return 1

        # Strongest claim first: a pair that came back beats a pair that merely
        # racked matches up, which is the whole point of counting sessions.
        order = sorted(
            bonds.items(),
            key=lambda item: (not item[1].strong, -item[1].sessions, -item[1].matches, item[0]),
        )
        names_by_puuid = {puuid: name for puuid, name, _ in db.known_names(list(bonds))}
        me = _self_puuid(db)
        cached = db.team_samples([entry["puuid"]]).get(entry["puuid"], 0)

        # Meeting somebody once is what matchmaking does to everybody, so those
        # are counted rather than listed: fifteen rows of strangers would bury
        # the two people the question was actually about.
        repeats = [item for item in order if item[1].matches >= party.MIN_SHARED]
        once = len(order) - len(repeats)
        if not repeats:
            render.console.print()
            render.warn(f"nobody turns up on their side twice in {cached} cached matches")
            render.info(f"{once} people were there exactly once - that is just matchmaking")
            render.info("python -m valstats backfill - parse more matches into the cache")
            return 0

        table = Table(title=f"Who {_name(entry)} plays with", title_style="bold", header_style="dim")
        for header, justify in (
            ("Player", "left"),
            ("Matches", "right"),
            ("Sessions", "right"),
            ("Last", "left"),
            ("Party", "left"),
        ):
            table.add_column(header, justify=justify, no_wrap=True)
        for other, bond in repeats[:limit]:
            table.add_row(
                _mate_name(other, names_by_puuid.get(other), me),
                str(bond.matches),
                str(bond.sessions) if bond.sessions else "?",
                _short(bond.last),
                "[bold magenta]likely[/bold magenta]" if bond.strong else "[dim magenta]thin[/dim magenta]",
            )
        render.console.print()
        render.console.print(table)
        render.info(
            f"from {cached} cached matches of theirs: {len(repeats)} met there more than once, "
            f"{once} met once. No requests were made"
        )
        render.info(
            "Riot publishes no player's friends list - this is who they actually queue with"
        )
        return 0
    finally:
        db.close()


def _mate_name(puuid, name, me):
    """One side-mate as the table should print them.

    The fallback is dim markup rather than a name, so it must not be escaped -
    and a real Riot ID must be, because brackets are legal in one.
    """
    if puuid == me:
        return "[bold]you[/bold]"
    return escape(name) if name else f"[dim]{puuid[:8]}...[/dim]"


def agents(limit=15):
    """Your own agent pool: what you play, and how it actually goes.

    This is the same record the pick advice reads in agent select, so it is
    worth a look if a suggestion there ever seems odd.
    """
    db = Encounters()
    try:
        config = load_config()
        content = Content().load(want_skins=False)
        client_puuid = _own_puuid(config)
        if not client_puuid:
            return 1
        pool = by_agent(db.all_perf_rows(client_puuid), load_calibration(db, "auto"))
        if not pool:
            render.warn("no cached matches of your own yet - run backfill first")
            return 1

        table = Table(title="Your agents", title_style="bold", header_style="dim")
        for header, justify in (
            ("Agent", "left"),
            ("Role", "left"),
            ("Matches", "right"),
            ("Score", "right"),
            ("ACS", "right"),
            ("K/D", "right"),
            ("WR", "right"),
        ):
            table.add_column(header, justify=justify, no_wrap=True)
        ranked = sorted(pool.items(), key=lambda kv: (-kv[1]["matches"], -kv[1]["rating"]))
        for agent, summary in ranked[:limit]:
            table.add_row(
                content.agent(agent),
                content.role(agent) or "-",
                str(summary["matches"]),
                str(summary["rating"]),
                f"{summary['acs']:.0f}" if summary["acs"] is not None else "-",
                f"{summary['kd']:.2f}",
                f"{summary['winrate']:.0f}%",
            )
        render.console.print()
        render.console.print(table)
        render.info(
            "matches cached before this version carry no agent and are not counted here"
        )
        return 0
    finally:
        db.close()


def _own_puuid(config):
    """Your puuid, which only the running client can tell us."""
    try:
        return Client(config).connect().puuid
    except ClientUnavailable as exc:
        render.error(f"{exc}")
        render.warn("Start the Riot Client and log in, then run this again.")
        return None


def backfill(count=20):
    """Parse your own recent matches so the first live lobby is not a cold start.

    Only your own history is read: what it fills in is the numbers, not the
    "seen before" counter. Finished matches do name everyone in them, though,
    so people you already met but only ever saw as [hidden] get their Riot ID
    filled in here too - the same thing the post-match scoreboard does.
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

        # Matches parsed before sides were cached hold no team, and party
        # detection cannot use them. Re-reading is one request each, and only
        # ever happens once - so it rides along with a backfill you asked for.
        stale = db.matches_missing_team()
        # Matches cached before Riot's own party ids were read out. That field
        # is what turns party detection from an inference into a fact, so it is
        # worth the same one-off re-read the sides got.
        stale |= db.matches_missing_party()
        # Matches cached before ranks were read out of the record are worth the
        # same one-off re-read: that field is what ranks a hidden player later.
        if config["reveal_after_match"]:
            stale |= db.matches_missing_ranks()

        reveal = config["reveal_after_match"]
        fetched = refetched = skipped = renamed = ranked = 0
        for index, match_id in enumerate(match_ids, start=1):
            if db.is_parsed(match_id) and match_id not in stale:
                skipped += 1
                continue
            revisit = match_id in stale
            try:
                details = client.match_details(match_id)
            except ClientUnavailable:
                details = client.match_details(match_id)
            if not details:
                continue
            parsed_id, per_player = extract(details)
            db.store_match_perf(parsed_id or match_id, per_player)
            db.store_match_meta(parsed_id or match_id, meta(details))
            if reveal:
                found = names(details)
                found.pop(client.puuid, None)
                renamed += db.remember_names(found)
                ranks = tiers(details)
                ranks.pop(client.puuid, None)
                ranked += db.store_rank_snapshots(parsed_id or match_id, ranks)
            if revisit:
                refetched += 1
            else:
                fetched += 1
            render.info(f"  {index}/{len(match_ids)} matches...", repeat=True)

        totals = db.totals()
        again = f", {refetched} re-read for sides, parties and ranks" if refetched else ""
        render.info(
            f"done: {fetched} matches downloaded{again}, {skipped} already cached; "
            f"{totals['lines']} player-lines in total"
        )
        if reveal:
            # Only the leaderboard source wants the content, and only for the
            # act uuid - so a content server having a bad day costs one source
            # out of five rather than the whole backfill.
            try:
                content = Content().load(want_skins=False)
            except Exception as exc:  # noqa: BLE001 - see _content in identify.py
                render.warn(f"no game content ({exc}) - the leaderboard source is off")
                content = None
            renamed += _name_the_hidden(client, db, config, content)
        if renamed:
            render.info(f"filled in the Riot ID of {renamed} players you had already met")
        if ranked:
            render.info(f"and the rank {ranked} of them were at when you played")
        # Every match just downloaded also names the nine people you played it
        # with, which is exactly what `who` lists.
        _link(db)
        return _calibration_note(db)
    finally:
        db.close()


def _name_the_hidden(client, db, config, content=None):
    """Put a name to everyone we have only ever seen as [hidden].

    Incognito blanks the name service for as long as you are in a match with
    the player, and those matches are long over: asking again now answers for
    most of them. Whoever is left goes round the rest of the sources - see
    identity.py - which is what `identify` does on its own, run here because a
    backfill has just finished collecting the puuids anyway.

    Only for people already in the local memory: this names someone we have
    met, it does not go looking.
    """
    puuids = db.nameless()
    if not puuids:
        return 0
    resolver = identity.Resolver.build(config, client=client, db=db, content=content)
    renamed = 0
    for start in range(0, len(puuids), NAME_BATCH):
        chunk = puuids[start : start + NAME_BATCH]
        try:
            found = resolver.resolve(chunk, phase="after")
        except (ClientUnavailable, requests.RequestException) as exc:
            render.warn(f"could not look the hidden players up: {exc}")
            break
        renamed += db.remember_found(found)
    return renamed


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
        _convergence_note(db)
        return 0
    finally:
        db.close()


def _convergence_note(db):
    """The other calibration: mmr.py's convergence constant. See mmr.calibrate.

    Printed next to the score's because the two answer the same sort of
    question about this install - what are the numbers on screen measured
    against - and because a constant that is quietly being measured rather
    than assumed is worth one line somewhere a person can go and look at it.
    """
    histories = db.rr_histories(mmr.MIN_CALIBRATION_MATCHES)
    render.console.print()
    render.console.print(
        f"[bold]Hidden-rating convergence[/bold]  [dim]{len(histories)} cached RR histories "
        f"at least {mmr.MIN_CALIBRATION_MATCHES} matches deep[/dim]"
    )
    measured = mmr.load_calibration(db, "auto")
    if not measured:
        # Two different numbers, and the gap between them is the answer to
        # "why is it still on the default": most histories are of somebody
        # sitting at their own rank, and a player who never moved cannot say
        # how fast moving happens.
        could = len(mmr.votes(histories.values()))
        render.warn(
            f"using the default {mmr.CONVERGENCE_MATCHES} matches a division - "
            f"{could} of those {len(histories)} could vote, "
            f"{mmr.MIN_CALIBRATION_PLAYERS} needed"
        )
        return
    render.info(
        f"{measured.matches:.1f} matches a division, measured on {measured.players} "
        f"histories (quartiles +-{measured.spread:.1f})"
    )


def _calibration_note(db):
    population = db.population(MIN_POPULATION_ROUNDS)
    if len(population) >= MIN_POPULATION and Calibration.from_population(population):
        render.info(f"score now calibrated against {len(population)} players")
    else:
        render.info(
            f"{len(population)}/{MIN_POPULATION} players cached towards percentile scoring"
        )
    return 0
