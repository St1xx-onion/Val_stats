"""Configuration loading."""

import json
from pathlib import Path

from . import identity, mmr

ROOT = Path(__file__).resolve().parent.parent

DEFAULTS = {
    "poll_interval": 1.0,
    "show_skins": True,
    "show_peak_rank": True,
    "respect_streamer_mode": True,
    "track_encounters": True,
    # ACS / HS% / K-D from recent matches. Costs extra requests on a cold cache.
    "fetch_performance": True,
    "performance_matches": 5,
    # Which queue recent form is read from, and whether to fall back to every
    # queue for players who simply have no competitive history.
    "performance_queue": "competitive",
    "performance_queue_fallback": True,
    # The rank the system is walking a player towards, read out of the RR each
    # of their ranked matches paid. See mmr.py. One request per player on top
    # of the lobby's usual cost, and the column simply stays empty for anyone
    # Riot will not answer about.
    "estimate_mmr": True,
    # Ranked matches the estimate is read from. More is a narrower band and no
    # more requests - Riot serves twenty in one call, and forty in two.
    "mmr_matches": 20,
    # A rank for the players in Incognito, read out of the record of their own
    # recent matches rather than out of a lookup. Incognito covers the live
    # lookup; it does not cover a match that has finished, which is why the
    # tracker sites can put a badge next to somebody who played the whole game
    # as [hidden]. The name stays hidden either way - see respect_streamer_mode.
    "rank_hidden_from_history": True,
    # How many of their matches to look through before giving up. It stops at
    # the first one that records a rank, so this is a ceiling and not a cost:
    # the usual answer is the newest match, two requests, and nothing at all
    # when the cache already has them.
    "rank_hidden_matches": 5,
    # And the form columns for those same players, read the same way: out of
    # their own finished matches, which is exactly what the full report does
    # and the one thing the live table used to leave to it. Same principle as
    # the rank above, one column further along - Incognito closes the live
    # lookup, not the record of a match that has already been played - and the
    # same cost as any other player, performance_matches matches deep and
    # nothing at all on a warm cache. It runs after the visible half of the
    # table is filled, so nobody waits on a stranger who is hiding, and the
    # rank comes free with it: every match it downloads carries the rank of
    # everybody who was in it, so a player the shallow read above could not
    # place is placed by this one at no further cost. The name stays hidden
    # either way: respect_streamer_mode is about the name.
    "form_hidden_from_history": True,
    # The two sides' chances under the live table. Arithmetic over numbers the
    # lobby has already paid for - no requests of its own. Shallower than the
    # full report's, because the form behind it is performance_matches deep
    # rather than deep_matches, and it says so on screen.
    "show_odds": True,
    # How the drift is turned into a distance. "auto" measures the convergence
    # constant against the RR histories already in the cache and falls back to
    # mmr.CONVERGENCE_MATCHES until there are enough of them; "fixed" stays on
    # the constant; a number pins it by hand. See mmr.calibrate. No requests
    # either way - this is arithmetic over rows already downloaded.
    "mmr_convergence": "auto",
    # How deep the full report digs, per player. Nothing else uses this: the
    # watcher still takes performance_matches, which is the point of it being
    # a separate number. Thirty is about three hundred requests for a whole
    # lobby on a cold cache, and nearly free on a warm one.
    "deep_matches": 30,
    # An extra pause between the full report's requests, in seconds, on top of
    # the standing request_gap. The report is the only thing here that queues
    # hundreds in a row, and it is the only thing nobody is waiting on.
    "deep_request_gap": 0.35,
    # Print a scoreboard for the match you just played, once it is over.
    "post_match_summary": True,
    # How long to keep asking Riot for the match that just ended, in seconds.
    # The record is published a beat after the game ends - and now and then a
    # good deal later than a beat. Only the first few seconds of this hold the
    # poll loop up; the rest happens between polls while you sit in menus.
    # Running out no longer loses the scoreboard: the match is queued in the
    # cache and asked for quietly from then on. This is only how long the
    # waiting is worth watching. 0 sends it straight to the queue.
    "summary_wait_seconds": 180.0,
    # How long a match that never published stays in that queue, in days. It
    # survives the next lobby and a restart of the program, and is retried
    # between matches and on every start. Riot has been slow; Riot has not been
    # slow for a week, so a match older than this is one that is never coming.
    # 0 keeps it forever.
    "summary_keep_days": 7.0,
    # Name the players who were hidden during the match on that scoreboard.
    # The record of a finished match carries everyone's Riot ID - Incognito
    # only covers the live lookup - which is what the tracker sites show.
    "reveal_after_match": True,
    # Where a Riot ID may be looked for, and in what order. See identity.py
    # for what each one is; "henrik" and "riot-account" are the only two that
    # send another player's puuid off this machine, and neither is here by
    # default. An empty list turns the whole thing off and leaves only the
    # plain name service.
    "reveal_sources": list(identity.DEFAULT_SOURCES),
    # How old the cached leaderboard dump may get before it is pulled again,
    # in hours. Fifteen requests each time, and never during a live lobby.
    "leaderboard_cache_hours": 12.0,
    # Keys for the two optional sources. Setting one is what enables it - it
    # also has to be listed in reveal_sources above.
    "henrik_api_key": None,
    "riot_api_key": None,
    # Work out who queued together, from matches already in the local cache.
    # Costs no requests; needs fetch_performance to have filled that cache.
    "detect_parties": True,
    # Same-side matches two players must share before they count as a party.
    # 1 finds more and invents more: in a small region the same faces recur.
    "party_min_shared": 2,
    # A break longer than this ends a play session, in hours. Two matches an
    # hour apart are one decision to queue together; two on separate evenings
    # are two, and that gap is most of what tells a party from a coincidence.
    "party_session_gap_hours": 3.0,
    # Suggest agents in agent select, from the roles your team is missing and
    # your own record on each agent.
    "recommend_picks": True,
    # Only suggest agents this account has unlocked. Costs one request per
    # session; without it an agent you do not own can be recommended.
    "recommend_owned_agents_only": True,
    # How often to re-read agent select while it is open, in seconds, so the
    # advice reacts to allies locking in. One request each time; 0 turns it off.
    "pregame_refresh": 5.0,
    # How often a chat presence that says MENUS is checked against the game
    # servers, in seconds. The presence can come back from a chat reconnect
    # stuck on MENUS and stay there through a whole match; this is what catches
    # that. Two requests each time; 0 turns it off and trusts the presence.
    "menus_verify": 30.0,
    # How often to print a line saying we are still here while nothing is
    # happening, in minutes. Silence and a wedged process look the same on a
    # console. 0 turns it off.
    "heartbeat_minutes": 10.0,
    # "auto" scores players against everyone this install has ever met, once
    # enough of them are cached; "fixed" always uses the bands in perf.rating().
    "rating_calibration": "auto",
    # How long a name / MMR / match-history answer stays good for, in minutes.
    # Agent select and the match itself would otherwise ask for all of it twice.
    "player_cache_minutes": 20,
    # Post the finished matches this install parsed to a shared pool, and use
    # what other installs posted. Off unless you turn it on, and worth reading
    # valstats/share.py before you do: it lists exactly what leaves the machine
    # (match facts, hashed ids) and what never does (names, tokens, ranks).
    "share_stats": False,
    # Where those matches go. The relay that holds the credentials for the pool
    # - never a GitHub URL, because writing to GitHub needs a secret and a
    # secret in a program you hand out is not a secret. See server/README.md.
    "share_endpoint": "",
    # Salt for the hashes that stand in for puuids in the pool. Empty means the
    # public one in share.py. Setting your own forks the pool: the same player
    # hashes differently under a different salt, so only installs that share
    # your salt can pool with you.
    "share_salt": "",
    # Download the shared pool and use it alongside the local cache. Reading is
    # separate from sharing on purpose: taking the pool does not oblige you to
    # send to it, and sending does not oblige you to take it.
    "use_pool": False,
    # "confirmed" takes only the matches two independent installs both sent;
    # "all" takes everything the pool holds, corroborated or not.
    "pool_trust": "confirmed",
    # How old the downloaded copy may get before it is fetched again, in hours.
    # One request, and only between matches - never during a lobby.
    "pool_refresh_hours": 12.0,
    # Look on GitHub for a newer version at startup, and offer it. One request
    # a day at most, and never during a match. An update replaces code only -
    # config.json, encounters.db and cache/ are never written by it.
    "check_updates": True,
    # "owner/repo" to check. Empty turns the whole thing off, which is what a
    # copy that was not handed out from GitHub wants.
    "update_repo": "",
    # Install without asking. Off, because saying yes should be somebody's
    # decision rather than a default they never saw.
    "auto_update": False,
    # How often to look, in hours.
    "update_check_hours": 24.0,
    # Minimum gap between two outbound requests, in seconds.
    "request_gap": 0.15,
    # Leave null to auto-detect from ShooterGame.log
    "region": None,
    "shard": None,
    "client_version": None,
}

# Keys whose default is null and so says nothing about the type we accept.
FREE_FORM = {"region", "shard", "client_version", "henrik_api_key", "riot_api_key"}

# Settings that are a word or a number, and mean different things by each. The
# type check below works off the default's type, which cannot express that.
WORD_OR_NUMBER = {"mmr_convergence"}


def _type_ok(value, default):
    if isinstance(default, bool):
        return isinstance(value, bool)
    if isinstance(default, (int, float)):
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return isinstance(value, type(default))


def _convergence(value, warn):
    """mmr_convergence as mmr.load_calibration wants it, or the default.

    A number out of the sane range is worth a word rather than a silent
    correction: somebody who wrote 200 there meant something by it, and
    quietly measuring instead would leave them reading a report that does not
    do what they asked.
    """
    if isinstance(value, str):
        if value in ("auto", "fixed"):
            return value
        warn('[config] mmr_convergence must be "auto", "fixed" or a number - using "auto"')
        return "auto"
    if not mmr.MIN_CONVERGENCE <= float(value) <= mmr.MAX_CONVERGENCE:
        warn(
            f"[config] mmr_convergence {value} is outside "
            f"{mmr.MIN_CONVERGENCE:.0f}-{mmr.MAX_CONVERGENCE:.0f} matches - using \"auto\""
        )
        return "auto"
    return float(value)


def _sources(value, warn):
    """reveal_sources, with the typos and the duplicates taken out.

    A misspelled source is worth saying out loud for the same reason a
    misspelled setting is: silently looking a player up in four places when
    you asked for five is not a failure anybody would notice.
    """
    cleaned = []
    for name in value or ():
        if not isinstance(name, str):
            warn(f"[config] reveal_sources should hold names, not {name!r} - ignored")
            continue
        key = name.strip().lower()
        if key not in identity.KNOWN_SOURCES:
            known = ", ".join(identity.KNOWN_SOURCES)
            warn(f"[config] unknown reveal source {name!r} - ignored. Known: {known}")
            continue
        if key not in cleaned:
            cleaned.append(key)
    return cleaned


def validate(raw, warn=print):
    """Merge user settings over the defaults, saying so when one makes no sense.

    A silently ignored typo is the worst outcome here: "fetch_perfomance" would
    just quietly do nothing at all.
    """
    cfg = dict(DEFAULTS)
    for key, value in (raw or {}).items():
        if key not in DEFAULTS:
            warn(f"[config] unknown setting {key!r} - ignored")
            continue
        default = DEFAULTS[key]
        if key in FREE_FORM:
            if value is not None and not isinstance(value, str):
                warn(f"[config] {key} should be text or null - using the default")
                continue
        elif key in WORD_OR_NUMBER:
            if isinstance(value, bool) or not isinstance(value, (str, int, float)):
                warn(f"[config] {key} should be text or a number - using the default")
                continue
        elif not _type_ok(value, default):
            want = type(default).__name__
            warn(f"[config] {key} should be {want} - using the default {default!r}")
            continue
        cfg[key] = value

    if cfg["poll_interval"] < 0.2:
        warn("[config] poll_interval below 0.2s is pointless - raised to 0.2")
        cfg["poll_interval"] = 0.2
    # It ends up in a URL, so it has to be a whole number.
    cfg["performance_matches"] = int(cfg["performance_matches"])
    if cfg["performance_matches"] < 1:
        warn("[config] performance_matches must be at least 1 - using 1")
        cfg["performance_matches"] = 1
    cfg["rank_hidden_matches"] = int(cfg["rank_hidden_matches"])
    if cfg["rank_hidden_matches"] < 1:
        warn("[config] rank_hidden_matches must be at least 1 - using 1")
        cfg["rank_hidden_matches"] = 1
    cfg["party_min_shared"] = int(cfg["party_min_shared"])
    if cfg["party_min_shared"] < 1:
        warn("[config] party_min_shared must be at least 1 - using 1")
        cfg["party_min_shared"] = 1
    cfg["party_session_gap_hours"] = float(cfg["party_session_gap_hours"])
    if cfg["party_session_gap_hours"] <= 0:
        # Zero would make every match its own session and every pairing look
        # like a party, which is the one answer the session count must not give.
        warn("[config] party_session_gap_hours must be above 0 - using 3")
        cfg["party_session_gap_hours"] = 3.0
    cfg["reveal_sources"] = _sources(cfg["reveal_sources"], warn)
    if cfg["leaderboard_cache_hours"] < 0:
        cfg["leaderboard_cache_hours"] = 0.0
    if cfg["rating_calibration"] not in ("auto", "fixed"):
        warn('[config] rating_calibration must be "auto" or "fixed" - using "auto"')
        cfg["rating_calibration"] = "auto"
    cfg["mmr_convergence"] = _convergence(cfg["mmr_convergence"], warn)
    if cfg["request_gap"] < 0:
        cfg["request_gap"] = 0.0
    if 0 < cfg["pregame_refresh"] < 2:
        warn("[config] pregame_refresh below 2s just burns requests - raised to 2")
        cfg["pregame_refresh"] = 2.0
    if cfg["pregame_refresh"] < 0:
        cfg["pregame_refresh"] = 0.0
    if 0 < cfg["menus_verify"] < 10:
        warn("[config] menus_verify below 10s just burns requests - raised to 10")
        cfg["menus_verify"] = 10.0
    if cfg["menus_verify"] < 0:
        cfg["menus_verify"] = 0.0
    if cfg["summary_wait_seconds"] < 0:
        cfg["summary_wait_seconds"] = 0.0
    cfg["summary_keep_days"] = float(cfg["summary_keep_days"])
    if cfg["summary_keep_days"] < 0:
        cfg["summary_keep_days"] = 0.0
    if cfg["heartbeat_minutes"] < 0:
        cfg["heartbeat_minutes"] = 0.0
    return cfg


def load():
    path = ROOT / "config.json"
    raw = {}
    if path.is_file():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print(f"[config] ignoring config.json: {exc}")
            raw = {}
    if not isinstance(raw, dict):
        print("[config] config.json should hold an object - ignoring it")
        raw = {}
    return validate(raw)
