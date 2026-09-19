"""Check every link in the chain without waiting for a match.

    python -m valstats.selftest
"""

from . import render
from .client import Client, ClientUnavailable
from .config import load as load_config
from .content import Content


def main():
    config = load_config()

    render.info("1/6  static content from valorant-api.com ...")
    content = Content().load(want_skins=config["show_skins"])
    render.info(
        f"     {len(content.agents)} agents, {len(content.tiers)} tiers, "
        f"{len(content.skins)} skins, current act {content.current_act}"
    )

    render.info("2/6  local Riot Client (lockfile + tokens) ...")
    try:
        client = Client(config).connect()
    except ClientUnavailable as exc:
        render.error(f"     FAILED: {exc}")
        render.warn("     Start the Riot Client and log in, then run this again.")
        return 1
    render.info(f"     puuid {client.puuid}")

    render.info("3/6  region / build ...")
    render.banner(client, content)

    render.info("4/6  session state + one authenticated request ...")
    state = client.session_state()
    render.info(f"     session state: {state}")
    if client.presence_stale:
        render.warn(
            "     the chat presence claimed MENUS and the game servers disagreed - "
            "going by the servers"
        )
    try:
        mmr = client.mmr(client.puuid)
    except ClientUnavailable:
        mmr = client.mmr(client.puuid)
    if not mmr:
        render.error("     MMR request came back empty - build string may be stale.")
        render.warn('     Launch VALORANT once, or set "client_version" in config.json.')
        return 1

    from .stats import parse_mmr

    parsed = parse_mmr(mmr, content.current_act)
    tier = content.tier(parsed["tier"])["name"]
    peak = content.tier(parsed["peak_tier"])["name"]
    render.info(
        f"     you: {tier} {parsed['rr']}RR, peak {peak}, "
        f"act record {parsed['wins']}/{parsed['games']}"
    )

    render.info("5/6  where a hidden player's Riot ID could come from ...")
    _report_sources(config, client, content)

    render.info("6/6  local memory ...")
    _report_memory(config)
    render.info("all good - run  python -m valstats  and start a match")
    return 0


def _report_sources(config, client, content):
    """Which of the name sources are switched on, and which cannot run.

    Worth its own step: a hidden player being named or not is the one thing in
    here that depends on five separate switches, and "it did not name anybody"
    should never be a mystery you have to read config.json to solve.
    """
    from . import identity
    from .db import Encounters

    db = Encounters(enabled=config["track_encounters"])
    try:
        resolver = identity.Resolver.build(config, client=client, db=db, content=content)
        if not resolver.sources:
            render.warn("     reveal_sources is empty - nobody hiding will ever be named")
            return
        for source in resolver.sources:
            if source.unavailable:
                render.info(f"     {source.label}: off - {source.unavailable}")
            else:
                render.info(f"     {source.label}: on")
        leaks = [s.label for s in resolver.usable("after") if s.key in ("henrik", "riot-account")]
        if leaks:
            render.warn(
                "     " + ", ".join(leaks) + " send other players' puuids off this "
                "machine - that is what having a key turned them on for"
            )
    finally:
        db.close()


def _report_memory(config):
    """How much the local cache knows, and what the score is measured against."""
    from .db import Encounters
    from .perf import MIN_POPULATION, MIN_POPULATION_ROUNDS, load_calibration

    db = Encounters(enabled=config["track_encounters"])
    try:
        if not db.enabled:
            render.info("     encounter tracking is off in config.json")
            return
        totals = db.totals()
        render.info(
            f"     {totals['players']} players met, {totals['matches']} matches parsed, "
            f"{totals['lines']} player-lines cached"
        )
        calibration = load_calibration(db, config["rating_calibration"])
        if calibration:
            render.info(f"     score calibrated against {calibration.players} players")
        else:
            qualified = len(db.population(MIN_POPULATION_ROUNDS))
            render.info(
                f"     score on the fixed bands - {qualified}/{MIN_POPULATION} players "
                "cached towards percentile scoring"
            )
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
