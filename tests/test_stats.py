"""Offline checks for the payload parsing - no game, no network needed.

    python -m tests.test_stats
"""

import base64
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from valstats import render  # noqa: E402
from valstats.client import Client, Pacer, presence_state, retry_delay  # noqa: E402
from valstats.config import validate  # noqa: E402
from valstats.db import Encounters  # noqa: E402
from valstats.perf import (  # noqa: E402
    BLANK,
    Calibration,
    aggregate,
    extract,
    outcome,
    rating,
    summarise,
)
from valstats.stats import (  # noqa: E402
    PlayerRow,
    attach_knives,
    parse_mmr,
    queue_label,
    rows_from_coregame,
    rows_from_pregame,
)


def _presence(product, loop_state=None, puuid="me"):
    private = ""
    if loop_state:
        blob = json.dumps({"sessionLoopState": loop_state}).encode()
        private = base64.b64encode(blob).decode()
    return {"puuid": puuid, "product": product, "private": private}

ACT = "act-uuid-current"

MMR = {
    "IsActRankBadgeHidden": False,
    "LatestCompetitiveUpdate": {"TierAfterUpdate": 17, "RankedRatingAfterUpdate": 42},
    "QueueSkills": {
        "competitive": {
            "SeasonalInfoBySeasonID": {
                "act-uuid-old": {
                    "CompetitiveTier": 20,
                    "NumberOfWins": 40,
                    "NumberOfGames": 70,
                    "WinsByTier": {"20": 3, "21": 1},
                },
                ACT: {
                    "CompetitiveTier": 17,
                    "RankedRating": 42,
                    "NumberOfWins": 12,
                    "NumberOfGames": 20,
                    "WinsByTier": {"16": 5, "17": 7},
                },
            }
        }
    },
}

PREGAME = {
    "ID": "pregame-1",
    "Teams": [
        {
            "TeamID": "Blue",
            "Players": [
                {
                    "Subject": "p1",
                    "CharacterID": "add6443a-41bd-e414-f6ad-e58d267f4e95",
                    "CompetitiveTier": 17,
                    "PlayerIdentity": {"AccountLevel": 210, "Incognito": False},
                },
                {
                    "Subject": "p2",
                    "CharacterID": "",
                    "CompetitiveTier": 0,
                    "PlayerIdentity": {
                        "AccountLevel": 88,
                        "Incognito": True,
                        "HideAccountLevel": True,
                    },
                },
            ],
        }
    ],
}

COREGAME = {
    "MatchID": "core-1",
    "MatchmakingData": {"QueueID": "competitive"},
    "Players": [
        {
            "Subject": "p1",
            "TeamID": "Blue",
            "CharacterID": "add6443a-41bd-e414-f6ad-e58d267f4e95",
            "PlayerIdentity": {"AccountLevel": 210},
        },
        {
            "Subject": "e1",
            "TeamID": "Red",
            "CharacterID": "unknown-uuid",
            "PlayerIdentity": {"AccountLevel": 55},
        },
        {"Subject": "coach", "TeamID": "Red", "IsCoach": True, "PlayerIdentity": {}},
    ],
}

MELEE = "2f59173c-4bed-b6c3-2191-dea9b58be9c7"
SOCKET = "bcef87d6-209b-46c6-8b19-fbe40bd95abc"

LOADOUTS = {
    "Loadouts": [
        {
            "Loadout": {
                "Subject": "p1",
                "Items": {MELEE: {"Sockets": {SOCKET: {"Item": {"ID": "skin-reaver"}}}}},
            }
        },
        {"Loadout": {"Subject": "e1", "Items": {}}},
    ]
}


DETAILS = {
    "matchInfo": {"matchId": "m1"},
    "teams": [{"teamId": "Blue", "won": True}, {"teamId": "Red", "won": False}],
    "players": [
        {
            "subject": "p1",
            "teamId": "Blue",
            "stats": {"score": 5000, "roundsPlayed": 20, "kills": 20, "deaths": 15, "assists": 5},
        },
        {
            "subject": "e1",
            "teamId": "Red",
            "stats": {"score": 3000, "roundsPlayed": 20, "kills": 10, "deaths": 20, "assists": 2},
        },
    ],
    "roundResults": [
        {
            "playerStats": [
                {
                    "subject": "p1",
                    "damage": [
                        {"receiver": "e1", "damage": 150, "headshots": 2, "bodyshots": 5, "legshots": 1},
                        {"receiver": "e1", "damage": 70, "headshots": 1, "bodyshots": 2, "legshots": 0},
                    ],
                },
                {"subject": "ghost-not-in-players", "damage": [{"headshots": 9}]},
            ]
        },
        {
            "playerStats": [
                {
                    "subject": "p1",
                    "damage": [{"receiver": "e1", "damage": 100, "headshots": 1, "bodyshots": 2}],
                }
            ]
        },
    ],
}

# Built to hit every KAST branch. k kills, a assists, s survives, t is traded
# once and left hanging once, x dies clean in round 1 then gets kills, d only dies.
KAST_DETAILS = {
    "matchInfo": {"matchId": "kast"},
    "teams": [{"teamId": "Blue", "won": True}],
    "players": [
        {"subject": s, "teamId": "Blue", "stats": {"roundsPlayed": 3}}
        for s in ("k", "a", "s", "t", "x", "d")
    ],
    "roundResults": [
        {
            "playerStats": [
                {"subject": "k", "kills": [
                    {"victim": "x", "killer": "k", "assistants": ["a"],
                     "timeSinceRoundStartMillis": 1000},
                    {"victim": "d", "killer": "k", "timeSinceRoundStartMillis": 1200},
                ]},
                {"subject": "a"}, {"subject": "s"}, {"subject": "t"},
                {"subject": "x"}, {"subject": "d"},
            ]
        },
        {
            # x kills t, then dies 1.5s later -> t was traded.
            "playerStats": [
                {"subject": "x", "kills": [{"victim": "t", "killer": "x",
                                            "timeSinceRoundStartMillis": 2000}]},
                {"subject": "k", "kills": [
                    {"victim": "x", "killer": "k", "timeSinceRoundStartMillis": 3500},
                    {"victim": "d", "killer": "k", "timeSinceRoundStartMillis": 4000},
                ]},
                {"subject": "a"}, {"subject": "s"}, {"subject": "t"}, {"subject": "d"},
            ]
        },
        {
            # Same shape, but the revenge kill lands 4s later - too late to trade.
            "playerStats": [
                {"subject": "x", "kills": [{"victim": "t", "killer": "x",
                                            "timeSinceRoundStartMillis": 1000}]},
                {"subject": "k", "kills": [
                    {"victim": "x", "killer": "k", "timeSinceRoundStartMillis": 5000},
                    {"victim": "d", "killer": "k", "timeSinceRoundStartMillis": 5200},
                ]},
                {"subject": "a"}, {"subject": "s"}, {"subject": "t"}, {"subject": "d"},
            ]
        },
    ],
}


class FakeContent:
    current_act = ACT
    agents = {"add6443a-41bd-e414-f6ad-e58d267f4e95": "Jett"}
    skins = {"skin-reaver": "Reaver Karambit"}

    def agent(self, uuid):
        if not uuid:
            return "-"
        return self.agents.get(uuid.lower(), "?")

    def tier(self, number):
        return {"name": f"tier{number}", "color": "ffffff"}

    def skin_name(self, items, weapon_uuid=MELEE):
        if not items:
            return "-"
        socket = (items.get(weapon_uuid) or {}).get("Sockets", {}).get(SOCKET)
        skin_id = ((socket or {}).get("Item") or {}).get("ID")
        return self.skins.get(skin_id, "-") if skin_id else "-"


def check(label, condition):
    print(f"{'PASS' if condition else 'FAIL'}  {label}")
    return bool(condition)


def quiet(*_args, **_kwargs):
    """Swallow the warnings config validation prints."""


# ------------------------------------------------------------------ sampling


def check_sampling():
    """Five matches is a small sample and the score has to admit it."""
    hot = {"acs": 300, "kast": 85, "dd": 60, "winrate": 100, "rounds": 13, "matches": 1}
    settled = dict(hot, rounds=1000, matches=50)
    cold = {"acs": 130, "kast": 45, "dd": -40, "winrate": 0, "rounds": 13, "matches": 1}

    ok = check("one hot match does not score 1000", 550 < rating(hot) < 700)
    ok &= check("one bad match does not score 0 either", 300 < rating(cold) < 450)
    ok &= check("a season's worth of rounds is believed", rating(settled) > 950)
    ok &= check("more evidence separates the two", rating(hot) < rating(settled))
    bare = {"acs": 300, "kast": 85, "dd": 60, "winrate": 70}
    ok &= check("no sample size means take the numbers at face value", rating(bare) == 1000)
    return ok


def check_calibration():
    """Percentile scoring against the players this install has actually met."""
    population = [
        {
            "acs": 100.0 + index,
            "kast": 40.0 + index * 0.5,
            "dd": -40.0 + index,
            "winrate": 20.0 + index * 0.5,
        }
        for index in range(80)
    ]
    calib = Calibration.from_population(population)
    ok = check("calibration builds once enough players are cached", calib is not None)
    ok &= check(
        "a handful of players is not a population",
        Calibration.from_population(population[:10]) is None,
    )
    ok &= check("the median player sits at half the band", 120 <= calib.score("acs", 139.5) <= 130)
    ok &= check("the best player takes the whole band", calib.score("acs", 500) == 250.0)
    ok &= check("the worst takes none of it", calib.score("acs", 0) == 0.0)
    ok &= check("neutral is the population median", 135 <= calib.neutral("acs") <= 145)
    ok &= check(
        "percentile scoring still tops out at 1000",
        rating({"acs": 500.0, "kast": 100.0, "dd": 200.0, "winrate": 100.0}, calib) == 1000,
    )
    revived = Calibration.from_json(calib.to_json())
    ok &= check(
        "calibration survives the round trip through the db",
        revived is not None and revived.score("acs", 139.5) == calib.score("acs", 139.5),
    )
    ok &= check("a broken blob does not crash startup", Calibration.from_json("{oops") is None)
    return ok


def check_outcome():
    details = {
        "teams": [
            {"teamId": "Blue", "roundsWon": 13, "won": True},
            {"teamId": "Red", "roundsWon": 7, "won": False},
        ]
    }
    result = outcome(details)
    ok = check("round score read per team", result["rounds"] == {"Blue": 13, "Red": 7})
    ok &= check("winner read from the teams block", result["winners"] == ["Blue"])
    ok &= check("a payload with no teams does not crash", outcome({})["rounds"] == {})
    return ok


# -------------------------------------------------------------------- config


def check_config():
    cfg = validate(
        {
            "fetch_perfomance": False,
            "show_skins": "yes",
            "poll_interval": 0.01,
            "performance_queue": "unrated",
        },
        warn=quiet,
    )
    ok = check("a typo cannot silently disable a feature", cfg["fetch_performance"] is True)
    ok &= check("a wrong type falls back to the default", cfg["show_skins"] is True)
    ok &= check("poll interval has a floor", cfg["poll_interval"] == 0.2)
    ok &= check("a good setting comes through", cfg["performance_queue"] == "unrated")
    ok &= check("region may be null", validate({"region": None}, warn=quiet)["region"] is None)
    ok &= check(
        "rating_calibration is one of two words",
        validate({"rating_calibration": "sometimes"}, warn=quiet)["rating_calibration"] == "auto",
    )
    ok &= check("a non-object config.json is ignored", validate(None, warn=quiet)["show_skins"])
    return ok


# ------------------------------------------------------------------ database


def check_db():
    db = Encounters(path=":memory:")
    rows = [
        PlayerRow(puuid="p1", name="Someone#EUW", tier=17, rr=42),
        PlayerRow(puuid="p2", name="", tier=0),
        PlayerRow(puuid="me", name="Me#1", is_self=True),
    ]
    db.record("m1", rows)
    db.record("m1", rows)  # the same match re-rendered must not count twice
    ok = check("a match is counted once", db.counts(["p1"]).get("p1") == 1)
    ok &= check("you are not an encounter", "me" not in db.counts(["me", "p1"]))

    rows[0].rr = 55
    db.record("m2", rows)
    ok &= check("meeting again counts", db.counts(["p1"]).get("p1") == 2)
    ok &= check(
        "rank is snapshotted per match", [s["rr"] for s in db.snapshots("p1")] == [42, 55]
    )
    ok &= check("unranked players are not snapshotted", db.snapshots("p2") == [])

    found = db.find("Some")
    ok &= check("players are searchable by name", bool(found) and found[0]["puuid"] == "p1")
    ok &= check("first seen is kept", bool(found[0]["first_seen"]))
    ok &= check("most met comes back ordered", db.most_met(1)[0]["puuid"] == "p1")

    db.store_match_perf("d1", {"x": dict(BLANK, score=1250, rounds=10, kast=8, won=1)})
    db.store_match_perf("d2", {"x": dict(BLANK, score=11250, rounds=30, kast=20, won=0)})
    ok &= check("a parsed match is remembered as parsed", db.is_parsed("d1"))
    ok &= check("every cached line comes back", len(db.all_perf_rows("x")) == 2)
    people = db.population(min_rounds=40)
    ok &= check("population weights by rounds", round(people[0]["acs"], 1) == 312.5)
    ok &= check("thin players are left out of the population", db.population(min_rounds=41) == [])

    db.store_calibration("blob", 61)
    ok &= check("calibration is remembered", db.calibration() == ("blob", 61))
    db.close()

    off = Encounters(enabled=False)
    ok &= check("a disabled db answers without a file", off.counts(["p1"]) == {})
    return ok


# -------------------------------------------------------------------- client


def check_client():
    calls = []

    client = Client(validate({"request_gap": 0}, warn=quiet))
    client.shard = "eu"

    def fake_remote(_method, url, **_kwargs):
        calls.append(url)
        return [
            {"Subject": "p1", "GameName": "Someone", "TagLine": "EUW"},
            {"Subject": "p2", "GameName": "", "TagLine": ""},
        ]

    client.remote = fake_remote
    first = client.names(["p1", "p2"])
    second = client.names(["p1", "p2"])
    ok = check("names are asked for once per session", len(calls) == 1)
    ok &= check("the cached answer is the fetched one", first == second)
    ok &= check("a hidden player stays blank in the cache", second["p2"] == "")

    client.mmr("p1")
    client.mmr("p1")
    ok &= check("MMR is memoised too", len(calls) == 2)
    client.mmr("p1", fresh=True)
    ok &= check("fresh=True goes back to Riot", len(calls) == 3)

    class Limited:
        headers = {"Retry-After": "7"}

    ok &= check("a 429 honours Retry-After", retry_delay(Limited(), 0) == 7.0)
    ok &= check("without the header it backs off", retry_delay(None, 2) == 8.0)
    ok &= check("a nonsense header still gives a delay", retry_delay(Limited(), 0) > 0)

    pacer = Pacer(gap=0.05)
    pacer.wait()
    started = time.monotonic()
    pacer.wait()
    ok &= check("the pacer spaces requests out", time.monotonic() - started >= 0.04)
    return ok


# ----------------------------------------------------------------- main loop


def check_loop():
    """The bits of the match loop that can be checked without a client."""
    from valstats.app import App

    won = {"rounds": {"Blue": 13, "Red": 7}, "winners": ["Blue"]}
    ok = check("a win is announced as one", App._result_title(won, "Blue") == "MATCH OVER - WON 13:7")
    ok &= check(
        "the same match from the other side",
        App._result_title(won, "Red") == "MATCH OVER - LOST 7:13",
    )
    draw = {"rounds": {"Blue": 12, "Red": 12}, "winners": []}
    ok &= check("a draw is not a loss", App._result_title(draw, "Blue") == "MATCH OVER - DRAW 12:12")
    ok &= check("an unknown side still shows the score", "12" in App._result_title(draw, None))

    me = PlayerRow(puuid="me", team="Blue", is_self=True)
    ally = PlayerRow(puuid="a", team="Blue")
    enemy = PlayerRow(puuid="e", team="Red")
    order = [r.puuid for r in App._perf_order([me, ally, enemy], "Blue")]
    ok &= check("enemies are filled in first, you last", order == ["e", "a", "me"])
    return ok


# -------------------------------------------------------------------- render


def check_render(content):
    row = PlayerRow(puuid="p1", team="Blue", name="Someone#EUW", tier=17, level=100)
    row.knife = "Reaver Karambit"
    played = dict(BLANK, score=2500, rounds=10, kills=15, deaths=10, assists=5, won=1)
    row.apply_performance(aggregate([played, played, played]))
    cells = render._row_cells(row, content, "Blue", show_perf=True)
    kept, dropped = render._fit([cells], render._wanted(True, True), 40)
    ok = check("a narrow window drops the knife first", dropped[0] == "Knife")
    ok &= check(
        "the columns worth having never drop",
        {"team", "agent", "player", "rank", "acs", "kd"} <= set(kept),
    )
    ok &= check("a wide window keeps everything", not render._fit([cells], kept, 400)[1])

    thin = PlayerRow(puuid="p2")
    thin.acs, thin.kd, thin.rating, thin.perf_matches = 300.0, 2.0, 900, 1
    ok &= check(
        "a one-match sample is shown quietly",
        "dim" in str(render._perf_cells(thin)["acs"].style),
    )
    ok &= check(
        "a full sample is shown normally",
        "dim" not in str(render._perf_cells(row)["acs"].style),
    )

    row.final = summarise([dict(BLANK, score=2500, rounds=10, kills=15, deaths=10, assists=5)])
    table = render.build_summary_table([row], "MATCH OVER - WON 13:7", own_team="Blue")
    ok &= check("the post-match table has a row per player", table.row_count == 1)
    return ok


def main():
    content = FakeContent()
    ok = True

    parsed = parse_mmr(MMR, ACT)
    ok &= check("current tier from the active act", parsed["tier"] == 17)
    ok &= check("current RR", parsed["rr"] == 42)
    ok &= check("act record", (parsed["wins"], parsed["games"]) == (12, 20))
    ok &= check("peak comes from WinsByTier across acts", parsed["peak_tier"] == 21)
    ok &= check("act badge not hidden", parsed["peak_hidden"] is False)

    empty = parse_mmr(None, ACT)
    ok &= check("empty MMR degrades quietly", empty["tier"] == 0 and empty["peak_tier"] == 0)

    unplayed = parse_mmr({"LatestCompetitiveUpdate": {"TierAfterUpdate": 9}}, ACT)
    ok &= check("falls back to LatestCompetitiveUpdate", unplayed["tier"] == 9)

    pre = rows_from_pregame(PREGAME, content)
    ok &= check("pregame returns the ally team", len(pre) == 2)
    ok &= check("locked agent resolved", pre[0].agent == "Jett")
    ok &= check("unlocked agent shown as dash", pre[1].agent == "-")
    ok &= check("incognito flagged", pre[1].hidden is True)
    ok &= check("hidden account level suppressed", pre[1].level == 0)

    core = rows_from_coregame(COREGAME, content)
    ok &= check("coach excluded from the lobby", len(core) == 2)
    ok &= check("unknown agent uuid does not crash", core[1].agent == "?")

    attach_knives(core, LOADOUTS, content)
    ok &= check("knife skin resolved by puuid", core[0].knife == "Reaver Karambit")
    ok &= check("missing loadout gives a dash", core[1].knife == "-")
    attach_knives(core, None, content)  # must not raise

    core[0].wins, core[0].games = 12, 20
    ok &= check("winrate", round(core[0].winrate) == 60)
    ok &= check("winrate is None with no games", core[1].winrate is None)

    match_id, per_player = extract(DETAILS)
    ok &= check("match id read from matchInfo", match_id == "m1")
    ok &= check("every player in the match is stored", set(per_player) == {"p1", "e1"})
    p1 = per_player["p1"]
    ok &= check("shot placement summed across rounds", (p1["hs"], p1["bs"], p1["ls"]) == (4, 9, 1))
    ok &= check("players absent from the roster are ignored", "ghost-not-in-players" not in per_player)
    ok &= check("opponent with no damage rows stays at zero", per_player["e1"]["hs"] == 0)

    ok &= check("damage dealt summed", p1["dmg_dealt"] == 320)
    ok &= check("damage credited to the receiver", per_player["e1"]["dmg_received"] == 320)
    ok &= check("win read from the teams block", (p1["won"], per_player["e1"]["won"]) == (1, 0))

    _, kast_players = extract(KAST_DETAILS)
    scored = {name: kast_players[name]["kast"] for name in ("k", "a", "s", "t", "x", "d")}
    ok &= check("kill counts toward KAST every round", scored["k"] == 3)
    ok &= check("assist and survival count", (scored["a"], scored["s"]) == (3, 3))
    ok &= check("traded death counts, late revenge does not", scored["t"] == 2)
    ok &= check("dying untraded scores nothing", scored["d"] == 0)
    ok &= check("a death does not cancel a kill in the same round", scored["x"] == 2)

    one = aggregate([p1])
    ok &= check("ACS is score over rounds", one["acs"] == 250.0)
    ok &= check("HS% over all shots", round(one["hs"], 1) == 28.6)
    ok &= check("K/D", round(one["kd"], 2) == 1.33)
    ok &= check("damage delta per round", one["dd"] == 16.0)
    ok &= check("win rate over matches", one["winrate"] == 100.0)

    ok &= check("rating maxes out at 1000", rating({"acs": 300, "kast": 85, "dd": 60, "winrate": 70}) == 1000)
    ok &= check("rating bottoms out at 0", rating({"acs": 130, "kast": 45, "dd": -40, "winrate": 20}) == 0)
    ok &= check("midpoint of every band is 500", rating({"acs": 215, "kast": 65, "dd": 10, "winrate": 45}) == 500)
    ok &= check("bands clamp above their top", rating({"acs": 999, "kast": 100, "dd": 500, "winrate": 100}) == 1000)
    ok &= check("rating rides along in the summary", one["rating"] == rating(one))

    # Weighted across matches, not an average of the per-match averages.
    short = {"score": 1200, "rounds": 10, "kills": 5, "deaths": 5, "assists": 3, "hs": 1,
             "bs": 9, "ls": 0, "kast": 6, "dmg_dealt": 1000, "dmg_received": 900, "won": 0}
    two = aggregate([p1, short])
    ok &= check("ACS weighted by rounds, not averaged", round(two["acs"], 1) == 206.7)
    ok &= check("match count reported", two["matches"] == 2)

    flawless = aggregate([{"score": 300, "rounds": 1, "kills": 3, "deaths": 0, "assists": 0,
                           "hs": 0, "bs": 0, "ls": 0, "kast": 1, "dmg_dealt": 300,
                           "dmg_received": 0, "won": 1}])
    ok &= check("zero deaths does not divide by zero", flawless["kd"] == 3.0)
    ok &= check("no shots gives HS% None", flawless["hs"] is None)
    ok &= check("empty input gives None", aggregate([]) is None)

    # The Riot Client publishes a stateless presence next to VALORANT's; reading
    # only the first one made every match look like VALORANT was not running.
    in_game = [_presence("riot_client"), _presence("valorant", "INGAME")]
    ok &= check(
        "VALORANT presence wins over the client's own",
        presence_state(in_game, "me") == ("INGAME", True),
    )
    ok &= check(
        "state survives the entries arriving in the other order",
        presence_state(list(reversed(in_game)), "me") == ("INGAME", True),
    )
    ok &= check(
        "client-only presence means the game is not running",
        presence_state([_presence("riot_client")], "me") == (None, False),
    )
    ok &= check(
        "a running game with no readable state is still flagged as running",
        presence_state([_presence("valorant")], "me") == (None, True),
    )
    ok &= check(
        "other people's presences are ignored",
        presence_state([_presence("valorant", "INGAME", puuid="other")], "me") == (None, False),
    )

    ok &= check(
        "known queue named",
        queue_label({"MatchmakingData": {"QueueID": "competitive"}}) == "Competitive",
    )
    ok &= check(
        "unknown queue title-cased",
        queue_label({"MatchmakingData": {"QueueID": "team_deathmatch"}}) == "Team Deathmatch",
    )
    ok &= check(
        "party modes fall back to the game mode, not to unrated",
        queue_label({"ModeID": "/Game/GameModes/Skirmish/SkirmishGameMode.SkirmishGameMode_C"})
        == "Skirmish",
    )
    ok &= check("no queue and no mode is a custom game", queue_label({}) == "custom game")

    ok &= check_sampling()
    ok &= check_calibration()
    ok &= check_outcome()
    ok &= check_config()
    ok &= check_db()
    ok &= check_client()
    ok &= check_loop()
    ok &= check_render(content)

    print("\n" + ("ALL CHECKS PASSED" if ok else "SOME CHECKS FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
