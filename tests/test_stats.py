"""Offline checks for the payload parsing - no game, no network needed.

    python -m tests.test_stats
"""

import base64
import json
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from valstats import identity, render  # noqa: E402
from valstats.client import (  # noqa: E402
    MAX_MEMO,
    PRESENCE_GRACE,
    STATE_GRACE,
    TOKEN_TTL,
    Client,
    ClientUnavailable,
    Pacer,
    presence_state,
    retry_delay,
)
from valstats.config import validate  # noqa: E402
from valstats.db import Encounters  # noqa: E402
from valstats.perf import (  # noqa: E402
    BLANK,
    Calibration,
    aggregate,
    by_agent,
    extract,
    meta,
    names,
    outcome,
    rating,
    summarise,
    tiers,
)
from valstats import conduct, mmr, odds  # noqa: E402
from valstats.party import Group, describe, detect  # noqa: E402
from valstats.picks import recommend, taken_agents  # noqa: E402
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


JETT = "add6443a-41bd-e414-f6ad-e58d267f4e95"
OMEN = "agent-omen"
SOVA = "agent-sova"
SAGE = "agent-sage"
KILLJOY = "agent-killjoy"
REYNA = "agent-reyna"
PHOENIX = "agent-phoenix"
ASCENT = "/game/maps/ascent/ascent"
HAVEN = "/game/maps/haven/haven"


class FakeContent:
    current_act = ACT
    agents = {
        JETT: "Jett",
        OMEN: "Omen",
        SOVA: "Sova",
        SAGE: "Sage",
        KILLJOY: "Killjoy",
        REYNA: "Reyna",
        PHOENIX: "Phoenix",
    }
    roles = {
        JETT: "Duelist",
        OMEN: "Controller",
        SOVA: "Initiator",
        SAGE: "Sentinel",
        KILLJOY: "Sentinel",
        REYNA: "Duelist",
        PHOENIX: "Duelist",
    }
    maps = {ASCENT: "Ascent", HAVEN: "Haven"}
    skins = {"skin-reaver": "Reaver Karambit"}

    def agent(self, uuid):
        if not uuid:
            return "-"
        return self.agents.get(uuid.lower(), "?")

    def role(self, uuid):
        return self.roles.get((uuid or "").lower(), "")

    def map_name(self, map_id):
        return self.maps.get((map_id or "").lower(), "")

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


def raises(kind, call):
    """True when `call` fails the way it is supposed to."""
    try:
        call()
    except kind:
        return True
    except Exception:  # noqa: BLE001 - the wrong exception is still a failure
        return False
    return False


class Reply:
    """As much of a requests response as the client actually touches."""

    def __init__(self, status=200, payload=None, broken=False):
        self.status_code = status
        self.headers = {}
        self.payload = payload
        self.broken = broken
        self.content = b"{}" if broken or payload is not None else b""

    def json(self):
        if self.broken:
            raise ValueError("truncated body")
        return self.payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code), response=self)


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
    ok &= check(
        "the post-match reveal is on unless it is turned off",
        validate({}, warn=quiet)["reveal_after_match"] is True,
    )
    ok &= check(
        "the menus check has a floor too",
        validate({"menus_verify": 2}, warn=quiet)["menus_verify"] == 10.0,
    )
    ok &= check(
        "but it can be turned off outright",
        validate({"menus_verify": 0}, warn=quiet)["menus_verify"] == 0.0,
    )
    ok &= check(
        "a negative heartbeat is simply no heartbeat",
        validate({"heartbeat_minutes": -5}, warn=quiet)["heartbeat_minutes"] == 0.0,
    )
    return ok


# ------------------------------------------------------------------ database


def _first(rows):
    return rows[0] if rows else {"ts": "", "num": 0}


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

    db.store_match_perf("d3", {"x": dict(BLANK, score=100, rounds=10, agent="agent-omen")})
    db.store_match_meta("d3", {"map_id": "/game/maps/ascent/ascent", "queue": "competitive"})
    played = {row["agent"]: row["map_id"] for row in db.all_perf_rows("x")}
    ok &= check("the agent survives the round trip", "agent-omen" in played)
    ok &= check("the map is joined back on", played["agent-omen"] == "/game/maps/ascent/ascent")
    ok &= check("a line cached without a map still comes back", None in played.values())

    met = db.met_in("p1")
    ok &= check("both meetings are named, newest first", [m["match_id"] for m in met] == ["m2", "m1"])
    ok &= check("every logged match gets a number", all(m["num"] for m in met))
    numbered = db.match_by_label(met[0]["num"])
    ok &= check("the number leads back to the match", numbered["match_id"] == "m2")
    ok &= check("an unknown number is not a match", db.match_by_label(9999) is None)
    ok &= check("the roster of a match comes back", {r["puuid"] for r in db.roster("m1")} == {"p1", "p2"})

    # A cached match of your own names everyone in it, even from before the log.
    for puuid in ("me", "old-friend"):
        db.store_match_perf("m0", {puuid: dict(BLANK, rounds=20)})
    db.store_match_meta("m0", {"queue": "competitive", "started_at": "2024-01-01T00:00:00+00:00"})
    db.store_match_perf("theirs", {"old-friend": dict(BLANK, rounds=20)})
    added = db.link_own_matches("me")
    ok &= check("an old lobby is recovered from the cache", added == 1)
    ok &= check("you are not recovered as your own encounter", db.met_in("me") == [])
    recovered = db.met_in("old-friend")
    ok &= check("a recovered meeting knows when it was", _first(recovered)["ts"][:4] == "2024")
    ok &= check(
        "matches downloaded for somebody else are not encounters",
        [m["match_id"] for m in recovered] == ["m0"],
    )
    ok &= check("recovering twice adds nothing", db.link_own_matches("me") == 0)

    db.remember("self_puuid", "me")
    ok &= check("small facts survive", db.setting("self_puuid") == "me")
    ok &= check("an unset fact falls back", db.setting("nothing", "-") == "-")

    db.store_calibration("blob", 61)
    ok &= check("calibration is remembered", db.calibration() == ("blob", 61))
    db.close()

    off = Encounters(enabled=False)
    ok &= check("a disabled db answers without a file", off.counts(["p1"]) == {})
    return ok


# ------------------------------------------------------------------- parties


def _lobby(*specs):
    """PlayerRow objects from (puuid, team) pairs."""
    return [PlayerRow(puuid=puuid, team=team) for puuid, team in specs]


def _sitting(day, count, hour=19):
    """Start times for `count` matches played back to back on one evening."""
    return [f"2026-03-{day:02d}T{hour + index:02d}:12:00+00:00" for index in range(count)]


def check_party():
    lobby = _lobby(("a", "Blue"), ("b", "Blue"), ("c", "Blue"), ("e1", "Red"), ("e2", "Red"))

    # a and b keep landing on the same side; c never does.
    duo = detect(lobby, {("a", "b"): 3, ("a", "c"): 1, ("b", "e1"): 4})
    ok = check("a repeated pairing is one group", len(duo) == 1)
    ok &= check("only the pair is in it", duo[0].members == ["a", "b"])
    ok &= check("the group is labelled", duo[0].label == "A")
    ok &= check("a single shared match is not a party", "c" not in duo[0].members)
    ok &= check(
        "players on opposite sides are never grouped, however often they meet",
        all("e1" not in g.members for g in duo),
    )

    trio = detect(lobby, {("a", "b"): 3, ("a", "c"): 2, ("b", "c"): 4})
    ok &= check("a full three-stack is one group", trio[0].members == ["a", "b", "c"])
    ok &= check("a complete group is solid", trio[0].solid is True)
    ok &= check("the weakest link is reported", trio[0].shared == 2)

    # a-b on Monday, b-c on Tuesday, a and c never together: connected, but
    # not the three-stack the component alone would claim.
    chain = detect(lobby, {("a", "b"): 3, ("b", "c"): 3})
    ok &= check("a chain is still reported", chain[0].members == ["a", "b", "c"])
    ok &= check("but not as a confident group", chain[0].solid is False)

    both = detect(lobby, {("a", "b"): 2, ("e1", "e2"): 2}, own_team="Blue")
    ok &= check("both sides are examined", len(both) == 2)
    ok &= check("your own side is lettered first", both[0].team == "Blue")
    ok &= check("letters are distinct", {g.label for g in both} == {"A", "B"})

    ok &= check("no evidence means no groups", detect(lobby, {}) == [])
    ok &= check("a lobby of solo queues stays empty", detect(lobby, {("a", "b"): 1}) == [])
    ok &= check(
        "the threshold is configurable",
        len(detect(lobby, {("a", "b"): 1}, min_shared=1)) == 1,
    )

    # ------------------------------------------------- occasions, not just counts
    two_evenings = detect(lobby, {("a", "b"): _sitting(1, 1) + _sitting(8, 1)})
    ok &= check("coming back on another evening is a party", two_evenings[0].strong is True)
    ok &= check("and the occasions are counted", two_evenings[0].sessions == 2)

    one_evening = detect(lobby, {("a", "b"): _sitting(1, 2)})
    ok &= check("two games in one sitting is only a lead", one_evening[0].strong is False)
    ok &= check("and reads as a single occasion", one_evening[0].sessions == 1)
    ok &= check("the pair is still reported", one_evening[0].members == ["a", "b"])

    ok &= check(
        "a run too long for matchmaking is a party on its own",
        detect(lobby, {("a", "b"): _sitting(1, 4)})[0].strong is True,
    )
    ok &= check(
        "a shorter gap splits one evening into several",
        detect(lobby, {("a", "b"): _sitting(1, 2)}, session_gap=0.5)[0].sessions == 2,
    )
    ok &= check(
        "the confident group is lettered first",
        detect(
            lobby,
            {("a", "b"): _sitting(1, 2), ("e1", "e2"): _sitting(1, 1) + _sitting(8, 1)},
        )[0].members
        == ["e1", "e2"],
    )

    # Matches cached before start times were stored carry none, and the count
    # alone is what is left to read.
    undated = detect(lobby, {("a", "b"): [None, None, None]})
    ok &= check("times we never recorded leave the occasions unknown", undated[0].sessions == 0)
    ok &= check("and a short undated run stays a lead", undated[0].strong is False)
    ok &= check(
        "while a long one is still a party",
        detect(lobby, {("a", "b"): [None] * 4})[0].strong is True,
    )

    from valstats.party import apply as stamp
    from valstats.party import weigh

    ok &= check("nothing shared weighs nothing", weigh([]) == (0, 0, "", False, 0))
    ok &= check(
        "a gap of exactly the limit is the same sitting",
        weigh(["2026-03-01T10:00:00+00:00", "2026-03-01T13:00:00+00:00"]).sessions == 1,
    )
    ok &= check(
        "a second past it is a new one",
        weigh(["2026-03-01T10:00:00+00:00", "2026-03-01T13:00:01+00:00"]).sessions == 2,
    )
    ok &= check(
        "a time with no zone on it is read as UTC, not thrown away",
        weigh(["2026-03-01T10:00:00"]).sessions == 1,
    )
    ok &= check("an unreadable time is dropped", weigh(["nonsense"]).sessions == 0)
    ok &= check("but the match it came from still counts", weigh(["nonsense"]).matches == 1)
    ok &= check(
        "the most recent shared match is dated",
        weigh(_sitting(1, 3)).last.startswith("2026-03-01T21"),
    )

    stamp(lobby, duo)
    stamped = {row.puuid: row.party for row in lobby}
    ok &= check("group members are stamped", stamped["a"] == stamped["b"] == "A?")
    ok &= check("a group resting on thin evidence is marked", stamped["a"].endswith("?"))
    ok &= check("everyone else is left blank", stamped["c"] == "")
    stamp(lobby, two_evenings)
    ok &= check(
        "a group worth believing is stamped plainly",
        {row.puuid: row.party for row in lobby}["a"] == "A",
    )
    stamp(lobby, [])
    ok &= check("a later lobby clears the old letters", not any(r.party for r in lobby))

    line = describe(trio, samples={"a": 5, "b": 9, "c": 12})
    ok &= check("the description names the group", "A: 3 players" in line)
    ok &= check("and what it could have shared at most", "2 of 5 shared" in line)
    ok &= check("a tentative group says so", "partly" in describe(chain))
    ok &= check("a dated group says over how many sittings", "over 2 sessions" in describe(two_evenings))
    ok &= check("one evening is called one", "in one sitting" in describe(one_evening))
    ok &= check("and admitted to be thin", "(thin)" in describe(one_evening))
    ok &= check("an undated group claims no sittings", "session" not in describe(undated))
    ok &= check("nothing found says nothing at all", describe([]) == "")
    return ok


def check_party_db():
    db = Encounters(path=":memory:")
    # One match: a, b and e together on Blue, e1 on Red.
    db.store_match_perf(
        "p1",
        {
            "a": dict(BLANK, rounds=20, team="Blue"),
            "b": dict(BLANK, rounds=20, team="Blue"),
            "e1": dict(BLANK, rounds=20, team="Red"),
        },
    )
    db.store_match_perf(
        "p2",
        {
            "a": dict(BLANK, rounds=20, team="Red"),
            "b": dict(BLANK, rounds=20, team="Red"),
            "e1": dict(BLANK, rounds=20, team="Blue"),
        },
    )
    # A third match where a and b were opponents - must not count.
    db.store_match_perf(
        "p3",
        {"a": dict(BLANK, rounds=20, team="Blue"), "b": dict(BLANK, rounds=20, team="Red")},
    )

    evidence = db.party_evidence(["a", "b", "e1"])
    ok = check("same-side matches are counted", evidence.get(("a", "b")) == 2)
    ok &= check("being opponents is not evidence", evidence.get(("a", "e1"), 0) == 0)
    ok &= check("pairs come back in sorted order", all(k[0] < k[1] for k in evidence))
    ok &= check(
        "the match on screen is left out",
        db.party_evidence(["a", "b"], exclude="p2").get(("a", "b")) == 1,
    )
    ok &= check("one player has no pairs", db.party_evidence(["a"]) == {})

    # The timeline behind that count: one entry per shared match, dated where
    # the cache happened to record a start time.
    db.store_match_meta("p1", {"started_at": "2026-03-01T19:00:00+00:00"})
    timeline = db.party_timeline(["a", "b"])
    ok &= check("the timeline holds one entry per shared match", len(timeline[("a", "b")]) == 2)
    ok &= check(
        "dated where a start time was recorded",
        "2026-03-01T19:00:00+00:00" in timeline[("a", "b")],
    )
    ok &= check("and left blank where none was", None in timeline[("a", "b")])

    mates = db.mates_of("a")
    ok &= check("a player's side-mates come back with their times", len(mates["b"]) == 2)
    ok &= check("an opponent is not a side-mate", "e1" not in mates)
    ok &= check("nobody is their own mate", "a" not in mates)
    ok &= check("a player we never cached has none", db.mates_of("nobody") == {})
    ok &= check(
        "a window drops the matches it cannot place",
        db.mates_of("a", since="2026-03-01T00:00:00+00:00")
        == {"b": ["2026-03-01T19:00:00+00:00"]},
    )

    ok &= check("cached sides are counted per player", db.team_samples(["a", "b"]) == {"a": 3, "b": 3})
    ok &= check("an unknown player has no sample", db.team_samples(["nobody"]) == {})

    # A line cached before the column existed carries no side at all.
    db.store_match_perf("old", {"a": dict(BLANK, rounds=20), "b": dict(BLANK, rounds=20)})
    ok &= check("a sideless match is not evidence", db.party_evidence(["a", "b"])[("a", "b")] == 2)
    ok &= check("and is offered up for a re-read", db.matches_missing_team() == {"old"})
    db.close()
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


# ------------------------------------------------------------------ identity


class FakeLocal:
    """As much of Client as the chat source touches."""

    def __init__(self, participants=(), friends=(), fails=False):
        self.payloads = {
            "/chat/v5/participants": {"participants": list(participants)},
            "/chat/v4/friends": {"friends": list(friends)},
        }
        self.fails = fails
        self.asked = []

    def local(self, _method, path):
        self.asked.append(path)
        if self.fails:
            raise ClientUnavailable("chat is gone")
        return self.payloads.get(path)


class FakeBoard:
    """A client that serves a leaderboard and counts how often it is asked."""

    pd = "https://pd.eu.a.pvp.net"
    region = "eu"
    shard = "eu"

    def __init__(self, players, page=None):
        self.players = list(players)
        self.page = page or identity.LEADERBOARD_PAGE
        self.calls = 0

    def remote(self, _method, url):
        self.calls += 1
        start = int(url.split("startIndex=")[1].split("&")[0])
        return {"totalPlayers": len(self.players), "Players": self.players[start : start + self.page]}


def _board_row(puuid, name, tag="1", anonymous=False):
    return {"puuid": puuid, "gameName": name, "tagLine": tag, "IsAnonymized": anonymous}


def check_identity():
    """Every source a Riot ID can come out of, and the order they are asked in."""
    ok = check("a live answer knows it is live", identity.Named("A#1", "chat").live)
    ok &= check(
        "and a dated one knows it is not",
        not identity.Named("A#1", "memory", "2026-01-01T00:00:00+00:00").live,
    )

    # The one source that answers with the game shut.
    db = Encounters(path=":memory:")
    db.record("m1", [PlayerRow(puuid="known", name="Known#1"), PlayerRow(puuid="shy", hidden=True)])
    found = identity.Memory(db).lookup(["known", "shy"], {})
    ok &= check("the local memory names who it has met", found["known"].name == "Known#1")
    ok &= check("and dates the claim", bool(found["known"].when))
    ok &= check("somebody it never named is not in it", "shy" not in found)
    ok &= check(
        "and with no database at all it says nothing",
        identity.Memory(None).lookup(["x"], {}) == {},
    )

    # Chat: local, free, and never taught about Incognito.
    chat = identity.Chat(
        FakeLocal(
            participants=[{"puuid": "shy", "game_name": "Shy", "game_tag": "GG"}],
            friends=[{"puuid": "pal", "game_name": "Pal", "game_tag": "EU"}],
        )
    )
    found = chat.lookup(["shy", "pal", "stranger"], {})
    ok &= check("a hidden teammate is named by the chat room", found["shy"].name == "Shy#GG")
    ok &= check("and a friend by the friends list", found["pal"].name == "Pal#EU")
    ok &= check("nobody else is invented", "stranger" not in found)
    ok &= check("a chat answer claims to be current", found["shy"].live)
    ok &= check(
        "a chat service that is gone is not a crash",
        identity.Chat(FakeLocal(fails=True)).lookup(["shy"], {}) == {},
    )

    # The match record, kept for the day Riot fills it in again.
    record = identity.MatchRecord()
    details = {"players": [{"subject": "shy", "gameName": "Shy", "tagLine": "GG"}]}
    ok &= check(
        "a record that still names people is read",
        record.lookup(["shy"], {"details": details})["shy"].name == "Shy#GG",
    )
    ok &= check(
        "the blank one Riot serves today gives nothing",
        record.lookup(["shy"], {"details": {"players": [{"subject": "shy", "gameName": ""}]}}) == {},
    )
    ok &= check("and no record at all is not a failure", record.lookup(["shy"], {}) == {})

    # The leaderboard: paged through, and an anonymised entry left alone.
    rows = [_board_row(f"p{n}", f"Player{n}") for n in range(5)]
    rows.append(_board_row("quiet", "Secret", anonymous=True))
    board_client = FakeBoard(rows, page=2)
    board = identity.Leaderboard(board_client, SimpleNamespace(current_act=ACT))
    board.page = 2
    index = board._dump()
    ok &= check("the whole board is paged through", len(index) == 5)
    ok &= check("every page after the first is asked for", board_client.calls >= 3)
    ok &= check("a puuid on it is named", index["p3"] == "Player3#1")
    ok &= check("an anonymised entry is left alone", "quiet" not in index)

    # A live lobby must never pay fifteen requests for this.
    cached = identity.Leaderboard(FakeBoard(rows), SimpleNamespace(current_act=ACT))
    cached._index, cached._fetched = {"p1": "Player1#1"}, "2026-01-01T00:00:00+00:00"
    live = cached.lookup(["p1"], {"phase": "live"})
    ok &= check("a live lobby reads the dump it already has", live["p1"].name == "Player1#1")
    ok &= check("and spends no request on refreshing it", cached.client.calls == 0)
    ok &= check("a name off the board is dated by the dump", live["p1"].when.startswith("2026-01"))
    ok &= check(
        "no act means no leaderboard to read",
        bool(identity.Leaderboard(board_client, SimpleNamespace(current_act=None)).unavailable),
    )

    # The two that leave this machine stay off until they are given a key.
    ok &= check("henrik is off without a key", bool(identity.Henrik(None).unavailable))
    ok &= check("and account-v1 too", bool(identity.RiotAccount("", "eu").unavailable))
    ok &= check("a key switches it on", not identity.RiotAccount("RGAPI-x", "eu").unavailable)
    ok &= check(
        "and the shard picks the routing host",
        identity.RiotAccount("RGAPI-x", "kr").routing == "asia",
    )
    ok &= check(
        "neither is in the default order",
        "henrik" not in identity.DEFAULT_SOURCES
        and "riot-account" not in identity.DEFAULT_SOURCES,
    )

    # The resolver: the first source that answers wins, the rest are spared.
    asked = []

    class Counting(identity.Source):
        def __init__(self, key, answers):
            self.key = self.label = key
            self.answers = answers

        def lookup(self, puuids, context):
            asked.append((self.key, tuple(puuids)))
            return {
                p: identity.Named(self.answers[p], self.key) for p in puuids if p in self.answers
            }

    resolver = identity.Resolver(
        [Counting("first", {"a": "A#1"}), Counting("second", {"a": "OLD#1", "b": "B#2"})]
    )
    found = resolver.resolve(["a", "b"])
    ok &= check("the first source that can name somebody does", found["a"].source == "first")
    ok &= check("the second picks up whoever is left", found["b"].name == "B#2")
    ok &= check("and is only ever asked about the leftovers", asked[1] == ("second", ("b",)))

    class Broken(identity.Source):
        key = label = "broken"

        def lookup(self, puuids, context):
            raise RuntimeError("this source is having a day")

    mixed = identity.Resolver([Broken(), Counting("works", {"a": "A#1"})])
    ok &= check(
        "one source blowing up does not lose the others",
        mixed.resolve(["a"])["a"].name == "A#1",
    )
    ok &= check(
        "a source that cannot run is not asked",
        identity.Resolver([identity.Memory(None)]).usable("after") == [],
    )
    ok &= check(
        "and neither is one that has no business in a live lobby",
        [s.key for s in identity.Resolver([identity.NameService(object())]).usable("live")] == [],
    )

    ok &= check(
        "the caption says where the names came from",
        identity.describe({"memory": 2, "chat": 1}, [identity.Memory(db), identity.Chat(None)])
        == "2 from local memory, 1 from client chat",
    )

    # A misspelled source is said out loud, not quietly dropped.
    warnings = []
    cfg = validate({"reveal_sources": ["memory", "memory", "leaderbored"]}, warn=warnings.append)
    ok &= check("a duplicate source is asked once", cfg["reveal_sources"] == ["memory"])
    ok &= check("and a typo is reported", any("leaderbored" in text for text in warnings))
    ok &= check(
        "an empty list is allowed - it means do not look",
        validate({"reveal_sources": []}, warn=quiet)["reveal_sources"] == [],
    )
    ok &= check(
        "the default order starts with the free ones",
        validate({}, warn=quiet)["reveal_sources"][:2] == ["memory", "chat"],
    )

    # Provenance: which source said so, which is what `who` reads back.
    db.remember_names({"shy": "Shy#GG"}, source="chat")
    db.remember_names({"shy": "Shy#GG"}, source="leaderboard")
    sources = {row[0] for row in db.name_provenance("shy")}
    ok &= check("every source that named a player is written down", sources == {"chat", "leaderboard"})
    ok &= check("and the name reaches the encounter itself", db.known_names(["shy"])[0][1] == "Shy#GG")
    ok &= check(
        "a Resolver answer goes in under its own source",
        db.remember_found({"known": identity.Named("Renamed#2", "henrik")}) == 1
        and db.name_provenance("known")[0][0] == "henrik",
    )

    # `identify <player>` asks every source about one puuid and prints the
    # answers side by side. What it must not do is file them under the name of
    # the source instead of the name of the player.
    from valstats import identify as identify_module

    board = identity.Leaderboard(FakeBoard([_board_row("lonely", "Lonely")]), SimpleNamespace(current_act=ACT))
    board._index, board._fetched = {"lonely": "Lonely#1"}, "2026-01-01T00:00:00+00:00"
    db.record("m2", [PlayerRow(puuid="lonely", hidden=True)])
    identify_module._one(db, identity.Resolver([board]), "lonely", "")
    filed = db.name_provenance("lonely")
    ok &= check("one player's answers are filed under that player", filed[0][1] == "Lonely#1")
    ok &= check("and under the source that gave them", filed[0][0] == "leaderboard")
    ok &= check("nothing is filed under a source name", db.name_provenance("leaderboard") == [])

    # And the live path: with respect_streamer_mode off, a lobby names whoever
    # the free sources can, and says where from - without spending a request.
    from valstats.app import App

    app = App.__new__(App)
    app.config = validate({"respect_streamer_mode": False}, warn=quiet)
    app.db = db
    app.named_live = ""
    app.identity = identity.Resolver([Counting("memory", {"shy": "Shy#GG"})])
    lobby = [
        PlayerRow(puuid="shy", hidden=True),
        PlayerRow(puuid="me", hidden=True, is_self=True),
        PlayerRow(puuid="nameless", hidden=True),
    ]
    ok &= check("the lobby names who it can", app._name_hidden(lobby) == 1)
    ok &= check("the hidden player gets their Riot ID", lobby[0].name == "Shy#GG")
    ok &= check("and stops being hidden in the table", lobby[0].hidden is False)
    ok &= check("the row remembers which source said so", lobby[0].name_source == "memory")
    ok &= check("you are never looked up as a hidden stranger", lobby[1].name == "")
    ok &= check("and nobody unnamed is invented", lobby[2].hidden is True)
    said = []
    app.view = SimpleNamespace(status="", update=lambda **kw: said.append(kw.get("status")))
    app._note_named()
    ok &= check("the line under the table says where the names came from",
                said == ["hidden players named: 1 from memory"])
    app._note_named()
    ok &= check("and is not repeated on the next redraw", len(said) == 1)

    # Harvesting is the quiet half: it writes the name down without printing
    # it, and it does so even when respect_streamer_mode leaves the table
    # honouring Incognito. This is what makes the post-match reveal reliable.
    app.config = validate({"respect_streamer_mode": True}, warn=quiet)
    app.identity = identity.Resolver([Counting("chat", {"quiet": "Quiet#RR"})])
    quiet_lobby = [PlayerRow(puuid="quiet", hidden=True)]
    db.record("m-harvest", quiet_lobby)
    named = app._harvest_hidden(quiet_lobby)
    ok &= check("the hidden name is captured", named == 1)
    ok &= check("straight into the local memory", db.known_names(["quiet"])[0][1] == "Quiet#RR")
    ok &= check("without ever unhiding them in the table", quiet_lobby[0].hidden is True)
    ok &= check("and without printing a name on the row", quiet_lobby[0].name == "")

    # And the mid-match re-read is throttled, so it does not poll chat every tick.
    app.last = ("m-harvest", quiet_lobby, None)
    app.harvest_at = time.monotonic()
    calls = []
    app._harvest_hidden = lambda rows: calls.append(rows)
    app.reharvest_hidden()
    ok &= check("a re-read too soon after the last is skipped", calls == [])
    app.harvest_at = time.monotonic() - app.HARVEST_EVERY - 1
    app.reharvest_hidden()
    ok &= check("but one after the interval goes through", len(calls) == 1)
    db.close()
    return ok

# ----------------------------------------------------------------- stability


def check_local_api():
    """Nothing the local client answers may reach the caller as a traceback."""
    client = Client(validate({"request_gap": 0}, warn=quiet))
    client.local_port, client.local_auth = "1234", "auth"
    replies = []
    client.session = SimpleNamespace(request=lambda *_a, **_k: replies.pop(0))

    replies.append(Reply(404))
    ok = check("a 404 is an answer, not an error", client.local("GET", "/x") is None)
    replies.append(Reply(401))
    ok &= check(
        "a refused password means the client restarted under us",
        raises(ClientUnavailable, lambda: client.local("GET", "/x")),
    )
    replies.append(Reply(500))
    ok &= check(
        "so does anything else that went wrong",
        raises(ClientUnavailable, lambda: client.local("GET", "/x")),
    )
    replies.append(Reply(200, broken=True))
    ok &= check("a truncated body is not worth a crash", client.local("GET", "/x") is None)

    client._tokens_at = time.monotonic()
    ok &= check("fresh tokens are left alone", client.keep_fresh() is False)
    client._tokens_at = time.monotonic() - TOKEN_TTL - 1
    replies.append(Reply(200, {"accessToken": "new", "token": "ent", "subject": "me"}))
    ok &= check("stale ones are replaced before they expire", client.keep_fresh() is True)
    ok &= check("and the fresh token is the one we hold", client.access_token == "new")

    hoarder = Client(validate({}, warn=quiet))
    for index in range(MAX_MEMO + 50):
        hoarder._memo_set(("name", index), "x")
    ok &= check("a session of days does not hoard memos", len(hoarder._memo) <= MAX_MEMO)
    return ok


def check_presence_dropout():
    """The chat service drops out; a match must not become invisible with it."""
    client = Client(validate({"request_gap": 0}, warn=quiet))
    client.puuid = "me"
    client.region, client.shard = "eu", "eu"
    chat = {"presences": [_presence("valorant", "INGAME")]}
    client.local = lambda *_a, **_k: chat

    probed = []

    def fake_remote(_method, url, **_kwargs):
        probed.append(url)
        return {"MatchID": "m1"} if "core-game" in url else None

    client.remote = fake_remote
    ok = check("the presence answers while it is there", client.session_state() == "INGAME")
    ok &= check("and costs nothing", not probed)

    chat["presences"] = []  # chat dropped; our own presence is simply gone
    ok &= check("a dropout falls back to the game servers", client.session_state() == "INGAME")
    ok &= check("which is one request, not a lost match", any("core-game" in u for u in probed))

    client._game_seen_at = time.monotonic() - PRESENCE_GRACE - 1
    ok &= check(
        "once the game has been gone a while it really is gone",
        client.session_state() is None,
    )

    # A probe that cannot reach Riot proves nothing, and must not be allowed
    # to prove "menus" - which is exactly what it used to do, because the
    # probe started at MENUS and looked for a match to contradict it.
    def broken_remote(_method, _url, **_kwargs):
        raise requests.ConnectionError("pd is unreachable")

    chat["presences"] = [_presence("valorant", "INGAME")]
    client.remote = fake_remote
    client._game_seen_at = time.monotonic()
    client._probe_at = 0.0
    ok &= check("a screen we were sure of is on record", client.session_state() == "INGAME")

    chat["presences"] = []
    client.remote = broken_remote
    client._probe_at = 0.0
    client._probe_result = "MENUS"
    ok &= check(
        "a probe that cannot reach Riot holds the last screen we knew",
        client.session_state() == "INGAME",
    )
    ok &= check("and does not pass it off as menus", client.session_state() != "MENUS")
    ok &= check("the failure is counted rather than swallowed", client.probe_failed >= 1)
    ok &= check(
        "and asks for the tokens to be re-read, in case that was it",
        client._tokens_at == 0.0,
    )

    client._state_at = time.monotonic() - STATE_GRACE - 1
    client._probe_at = 0.0
    ok &= check(
        "held only while it is recent enough to mean anything",
        client.session_state() is None,
    )

    # The same refusal, one layer down: a build string a patch has invalidated
    # answers 400 to everything, and 400 is not a 404.
    patched = Client(validate({"request_gap": 0, "client_version": "pinned"}, warn=quiet))
    patched.puuid = "me"
    patched.region, patched.shard = "eu", "eu"
    patched.session = SimpleNamespace(request=lambda *_a, **_k: Reply(400, None))
    ok &= check(
        "a lookup still reads a 400 as nothing found",
        patched.remote("GET", "https://glz/x") is None,
    )
    try:
        patched.remote("GET", "https://glz/x", strict=True)
        strict_raised = False
    except ClientUnavailable:
        strict_raised = True
    ok &= check("the state probe reads it as a refusal to answer", strict_raised)
    return ok


def _skirmish_presence(puuid="me"):
    """VALORANT's presence during a 2v2 skirmish, as a live client publishes it.

    Copied off a real one mid-match. The party and the queue are in there and
    `sessionLoopState` simply is not - which is the whole reason this test
    exists, because every other queue carries it.
    """
    blob = json.dumps(
        {
            "isValid": True,
            "partyId": "b2cdc7d0-737e-4d6e-99f1-21d8efc37125",
            "partySize": 1,
            "maxPartySize": 2,
            "provisioningFlow": "Matchmaking",
            "queueId": "skirmish2v2",
        }
    ).encode()
    return {"puuid": puuid, "product": "valorant", "private": base64.b64encode(blob).decode()}


def check_skirmish_state():
    """A 2v2 publishes no loop state at all, so the servers are the only source.

    The evening this was written, two windows both sat saying "still watching -
    in menus" through an entire 2v2. Neither was stuck and neither was wrong
    about the presence: the presence genuinely says nothing about which screen
    the game is on in that queue, so the state came from the probe alone - and
    the probe answered "menus" for both "there is no match" and "I could not
    ask", with no way for anything above it to tell the two apart.
    """
    client = Client(validate({"request_gap": 0}, warn=quiet))
    client.puuid = "me"
    client.region, client.shard = "eu", "eu"
    chat = {"presences": [_skirmish_presence()]}
    client.local = lambda *_a, **_k: chat

    ok = check(
        "a 2v2 presence says the game is up and nothing about where",
        presence_state(chat["presences"], "me") == (None, True),
    )

    live = {"match": "m1"}

    def fake_remote(_method, url, **_kwargs):
        if "core-game" in url and live["match"]:
            return {"MatchID": live["match"]}
        return None

    client.remote = fake_remote
    ok &= check("so the game servers answer for it", client.session_state() == "INGAME")

    # The failure that started all this. The match is still running; Riot has
    # stopped answering - an aged-out session, a patch-day build string, a 429
    # that ran out of retries. What must not happen is a confident "menus".
    def refuses(_method, _url, **_kwargs):
        raise ClientUnavailable("tokens were refused even after a refresh")

    client.remote = refuses
    client._probe_at = 0.0
    ok &= check(
        "a match that Riot stops answering about is not a match that ended",
        client.session_state() == "INGAME",
    )
    ok &= check("and it is not menus either", client.session_state() != "MENUS")

    # And the honest menus reading still works: asked, answered, no match.
    client.remote = fake_remote
    live["match"] = None
    client._probe_at = 0.0
    ok &= check("a real answer of 'no match' is still menus", client.session_state() == "MENUS")
    return ok


def check_stale_presence():
    """The presence says MENUS all through the match. The match still shows."""
    client = Client(validate({"request_gap": 0, "menus_verify": 10}, warn=quiet))
    client.puuid = "me"
    client.region, client.shard = "eu", "eu"
    chat = {"presences": [_presence("valorant", "MENUS")]}
    client.local = lambda *_a, **_k: chat

    live = {"match": None}
    probed = []

    def fake_remote(_method, url, **_kwargs):
        probed.append(url)
        if "core-game" in url and live["match"]:
            return {"MatchID": live["match"]}
        return None

    client.remote = fake_remote

    ok = check("a menu is checked against the servers", client.session_state() == "MENUS")
    ok &= check("which is what the two probe requests are for", len(probed) == 2)
    probed.clear()
    ok &= check("and then taken on trust for a while", client.session_state() == "MENUS")
    ok &= check("costing nothing in the meantime", not probed)

    live["match"] = "m1"  # the match starts, and the presence does not notice
    client._menus_checked_at -= 11
    client._probe_at = 0.0
    ok &= check(
        "a presence stuck on MENUS no longer hides a match",
        client.session_state() == "INGAME",
    )
    ok &= check("and the console has something to say about it", client.presence_stale == 1)

    probed.clear()
    client._probe_at = 0.0
    ok &= check("from there the servers lead", client.session_state() == "INGAME")
    ok &= check("without waiting the interval out again", any("core-game" in u for u in probed))
    ok &= check("one stuck presence is one episode, not one per poll", client.presence_stale == 1)

    live["match"] = None  # back in the menus, this time for real
    client._probe_at = 0.0
    ok &= check("agreeing again restores the presence", client.session_state() == "MENUS")
    probed.clear()
    ok &= check("and with it the free answer", client.session_state() == "MENUS")
    ok &= check("no requests at all", not probed)

    def unreachable(_method, _url, **_kwargs):
        raise requests.ConnectionError("glz is unreachable")

    client.remote = unreachable
    client._menus_checked_at -= 11
    client._probe_at = 0.0
    ok &= check(
        "a check that could not be made proves nothing",
        client.session_state() == "MENUS",
    )

    off = Client(validate({"request_gap": 0, "menus_verify": 0}, warn=quiet))
    off.puuid, off.region, off.shard = "me", "eu", "eu"
    off.local = lambda *_a, **_k: chat
    off.remote = unreachable
    ok &= check("turning the check off asks nobody", off.session_state() == "MENUS")
    return ok


def check_stale_version():
    """A VALORANT update used to read as "nothing is ever happening"."""

    class Found:
        @staticmethod
        def group(_number):
            return "new-build"

    client = Client(validate({"request_gap": 0}, warn=quiet))
    client.client_version = "old-build"
    client.access_token, client.entitlement = "token", "ent"
    client._search_log = lambda _pattern: Found

    replies = [Reply(400), Reply(200, {"MatchID": "m1"})]
    sent = []

    def request(_method, _url, headers=None, **_kwargs):
        sent.append(headers["X-Riot-ClientVersion"])
        return replies.pop(0)

    client.session = SimpleNamespace(request=request)
    ok = check(
        "a 400 is retried on a freshly read build string",
        client.remote("GET", "https://glz/x") == {"MatchID": "m1"},
    )
    ok &= check("and the stale one is not sent twice", sent == ["old-build", "new-build"])
    ok &= check("the client keeps the new one", client.client_version == "new-build")

    replies.append(Reply(400))
    sent.clear()
    ok &= check(
        "a 400 that is not about the build string is still an answer",
        client.remote("GET", "https://glz/x") is None,
    )
    ok &= check("asked once, since nothing changed", len(sent) == 1)

    pinned = Client(validate({"client_version": "pinned"}, warn=quiet))
    pinned._search_log = lambda _pattern: Found
    ok &= check("a version set by hand is left alone", pinned._refresh_version() is False)
    return ok


def check_recovery():
    """A tick that fails must not be the end of the watch."""
    import valstats.app as app_module
    from valstats.app import UNKNOWN, App, RESET_AFTER

    app = App.__new__(App)  # the real __init__ wants content, a db and a client
    app.config = validate({}, warn=quiet)
    app.client = "a connected client"
    app.state = "INGAME"
    app.rendered_match = "m1"
    app.retry, app.attempts, app.failures = True, 3, 0
    app.own_lines = []
    app.pregame_id, app.pregame_map, app.pregame_rows = "p1", "map", []
    app.view = render.MatchView(FakeContent())

    log = Path(tempfile.gettempdir()) / "valstats-test-errors.log"
    real_sleep, real_log = app_module.time.sleep, app_module.LOG_PATH
    app_module.time.sleep = lambda _seconds: None  # the loop backs off; a test should not
    app_module.LOG_PATH = log
    try:
        app.stumble("network hiccup: connection reset")
        ok = check("a network hiccup leaves the table alone", app.rendered_match == "m1")
        ok &= check("but it is counted", app.failures == 1)

        app.stumble("unexpected error: boom", ValueError("boom"))
        ok &= check("an unexpected error starts the screen over", app.state == UNKNOWN)
        ok &= check("so the next poll redraws the lobby", app.rendered_match is None)
        ok &= check("and the traceback is written down", log.is_file())
        ok &= check("in full", "ValueError: boom" in log.read_text(encoding="utf-8"))

        app.failures = RESET_AFTER - 1
        app.stumble("network hiccup: again")
        ok &= check("enough of them rebuild the connection", app.client is None)
        ok &= check("and the count starts over", app.failures == 0)
    finally:
        app_module.time.sleep = real_sleep
        app_module.LOG_PATH = real_log
        log.unlink(missing_ok=True)
    return ok


def check_lobby_retry():
    """A lobby that would not load is a slow lobby, not a lost match."""
    from valstats.app import MAX_ATTEMPTS, RETRY_AFTER, App

    app = App.__new__(App)
    app.retry, app.attempts, app.gave_up, app.retry_at = False, MAX_ATTEMPTS - 1, 0, 0.0

    app._after_attempt(False, "INGAME")
    ok = check("the quick attempts run out", app.retry is False)
    ok &= check("and another look is booked instead", app.retry_at > time.monotonic())
    first = app.retry_at - time.monotonic()
    ok &= check("soon enough to catch the same match", first <= RETRY_AFTER + 1)

    app.attempts = MAX_ATTEMPTS - 1
    app._after_attempt(False, "INGAME")
    ok &= check(
        "a lobby that keeps refusing is asked less often",
        app.retry_at - time.monotonic() > RETRY_AFTER,
    )

    app._after_attempt(True, "INGAME")
    ok &= check("and one that finally loads clears the score", app.gave_up == 0)

    drawn = []
    app.config = validate({"heartbeat_minutes": 0}, warn=quiet)
    app.client = SimpleNamespace(session_state=lambda: "INGAME", keep_fresh=lambda: False)
    # The resolver holds the client it was built with; a new stand-in
    # needs a new one, the same way reconnect() rebuilds it.
    app.identity = None
    app.state, app.presence_stale, app.probe_failed = "INGAME", 0, 0
    app.retry, app.attempts = False, 0
    app.pending_summary = None
    app.last, app.harvest_at = None, 0.0
    app.retry_at = time.monotonic() - 1
    app.beat_at = time.monotonic()
    app.show_coregame = lambda: (drawn.append("read") or True)
    app.tick()
    ok &= check("the booked look actually re-reads the lobby", drawn == ["read"])
    ok &= check("and is not left booked for ever", app.retry_at == 0.0)

    app.client = SimpleNamespace(session_state=lambda: "INGAME", keep_fresh=lambda: False)

    # The resolver holds the client it was built with; a new stand-in

    # needs a new one, the same way reconnect() rebuilds it.

    app.identity = None
    app.tick()
    ok &= check("an unchanged state still costs nothing", drawn == ["read"])
    return ok


def check_pending_summary():
    """A match Riot has not published yet is waited for, then queued - never dropped."""
    from valstats import app as app_module
    from valstats.app import App

    asked = []
    answers = [None, None, {"matchInfo": {}}]

    def fake_fetch(client, match_id, attempts=4, delay=3.0):
        asked.append(match_id)
        return answers.pop(0) if answers else None

    real_fetch = app_module.fetch_details
    app_module.fetch_details = fake_fetch
    db = Encounters(path=":memory:")
    queued = lambda: [entry["match_id"] for entry in db.pending_summaries()]
    try:
        app = App.__new__(App)
        app.config = validate({}, warn=quiet)
        app.client = None
        app.db = db
        app.pending_summary = None
        app.summary_quiet = False
        app.resume_at = 0.0
        printed = []

        def fake_summarise(match_id, *rest):
            # The real one clears the queue first thing, so the stub does too -
            # otherwise none of the queue assertions below would mean anything.
            printed.append((match_id, *rest))
            db.drop_summary(match_id)

        app._summarise = fake_summarise

        app._post_match("m1", [], None)
        ok = check("a match that is not published yet is kept", app.pending_summary is not None)
        ok &= check("and written down where a restart would find it", queued() == ["m1"])
        ok &= check("and nothing is printed for it yet", printed == [])

        app._poll_summary("MENUS")
        ok &= check("the very next poll is too soon to ask again", len(asked) == 1)

        app.summary_at = time.monotonic() - 1
        app._poll_summary("MENUS")
        ok &= check("once the gap has passed it asks", len(asked) == 2)
        ok &= check("and waits some more when the answer is still nothing", printed == [])
        ok &= check("the attempt is counted against the queued match",
                    db.pending_summaries()[0]["tries"] == 1)

        app.summary_at = time.monotonic() - 1
        app._poll_summary("MENUS")
        ok &= check("the scoreboard lands when Riot finally publishes it", len(printed) == 1)
        ok &= check("for the match it was waiting on", printed[0][0] == "m1")
        ok &= check("and is not asked for twice", app.pending_summary is None)
        ok &= check("and the queue lets it go", queued() == [])

        # Past summary_wait_seconds the watching stops - the waiting does not.
        app._post_match("m2", [], None)
        ok &= check("a second match waits the same way", app.pending_summary is not None)
        app.summary_at = app.summary_until = time.monotonic() - 1
        app._poll_summary("MENUS")
        ok &= check("outlasting the wait does not throw the match away", queued() == ["m2"])
        ok &= check("the asking just goes quiet", app.summary_quiet is True)
        ok &= check("and nothing is invented for it", len(printed) == 1)

        app._poll_summary("PREGAME")
        ok &= check("the next lobby lets the live rows go", app.pending_summary is None)
        ok &= check("but the match is still owed a scoreboard", queued() == ["m2"])

        # ...and that is what the between-matches sweep is for.
        answers.append({"matchInfo": {}})
        app._rows_from_details = lambda details: ([], "Blue")
        app.resume_at = 0.0
        app._resume_summaries("MENUS")
        ok &= check("a queued match is picked up between matches", len(printed) == 2)
        ok &= check("under the side it was queued with", printed[1][2] == "Blue")
        ok &= check("and leaves the queue once it lands", queued() == [])

        quiet_asks = len(asked)
        app.resume_at = 0.0
        app._resume_summaries("INGAME")
        ok &= check("the sweep never runs over a live lobby", len(asked) == quiet_asks)

        db.queue_summary("ancient", "Red")
        db.conn.execute(
            "UPDATE pending_summaries SET queued_at = ? WHERE match_id = 'ancient'",
            ("2020-01-01T00:00:00+00:00",),
        )
        db.conn.commit()
        app.resume_at = 0.0
        app._resume_summaries("MENUS")
        ok &= check("a match that never published is eventually let go", queued() == [])
        ok &= check("without asking Riot for it again", len(asked) == quiet_asks)

        app.config = validate({"summary_wait_seconds": 0}, warn=quiet)
        app.pending_summary = None
        app._post_match("m4", [], None)
        ok &= check("turning the watching off still queues the match", queued() == ["m4"])
    finally:
        app_module.fetch_details = real_fetch
        db.close()
    return ok


def check_rows_from_details():
    """A lobby rebuilt from the record alone, for a match that published late."""
    from valstats.app import App

    class FakeContent:
        def agent(self, agent_id):
            return {"aaa": "Jett", "bbb": "Sova"}.get(agent_id, "-")

    class FakeClient:
        puuid = "me"

    details = {
        "matchInfo": {"matchId": "m9"},
        "players": [
            {
                "subject": "me",
                "characterId": "AAA",
                "teamId": "Blue",
                "stats": {"score": 5000, "roundsPlayed": 20, "kills": 20},
            },
            {
                "subject": "known",
                "characterId": "bbb",
                "teamId": "Red",
                "stats": {"score": 4000, "roundsPlayed": 20, "kills": 15},
            },
            {
                "subject": "stranger",
                "characterId": "zzz",
                "teamId": "Red",
                "stats": {"score": 3000, "roundsPlayed": 20, "kills": 10},
            },
        ],
        "teams": [{"teamId": "Blue", "won": True}],
    }

    db = Encounters(path=":memory:")
    try:
        db.record("earlier", _lobby(("known", "Blue")))
        db.remember_names({"known": "Known#EUW"})

        app = App.__new__(App)
        app.db = db
        app.content = FakeContent()
        app.client = FakeClient()
        rows, own_team = app._rows_from_details(details)
        by_puuid = {row.puuid: row for row in rows}

        ok = check("every player in the record gets a row", len(rows) == 3)
        ok &= check("your own side is worked out from it", own_team == "Blue")
        ok &= check("the agent is read back through the content files",
                    by_puuid["known"].agent == "Sova")
        ok &= check("an agent id in either case is understood",
                    by_puuid["me"].agent == "Jett")
        ok &= check("the side comes off the record", by_puuid["known"].team == "Red")
        ok &= check("somebody the memory can name is named",
                    by_puuid["known"].name == "Known#EUW")
        ok &= check("and is not treated as hidden", by_puuid["known"].hidden is False)
        ok &= check("somebody it cannot is left for the reveal to name",
                    by_puuid["stranger"].hidden is True)
        ok &= check("you are marked as yourself", by_puuid["me"].is_self is True)
        ok &= check("and never hidden from yourself", by_puuid["me"].hidden is False)
    finally:
        db.close()
    return ok


def check_heartbeat():
    """Sitting quietly and hanging solid leave the same thing on screen."""
    from valstats.app import App

    app = App.__new__(App)
    app.config = validate({"heartbeat_minutes": 1}, warn=quiet)
    app.presence_stale = app.probe_failed = 0
    app.beat_at = time.monotonic()

    said = []
    real_info, render.info = render.info, lambda message, repeat=False: said.append(message)
    try:
        app._heartbeat("MENUS")
        ok = check("nothing is said while the last word is still recent", not said)
        app.beat_at = time.monotonic() - 61
        app._heartbeat("MENUS")
        ok &= check("but the watch reports in on the interval", len(said) == 1)
        ok &= check("saying it is still watching", "still watching" in said[0])
        app.beat_at = time.monotonic() - 61
        app._heartbeat("INGAME")
        ok &= check("and keeps quiet during a match", len(said) == 1)

        app.presence_stale, app.probe_failed = 1, 0
        app.beat_at = time.monotonic() - 61
        app._heartbeat("MENUS")
        ok &= check("a presence caught lying is mentioned again", "unreliable" in said[-1])

        app.config = validate({"heartbeat_minutes": 0}, warn=quiet)
        app.beat_at = time.monotonic() - 601
        app._heartbeat("MENUS")
        ok &= check("and it can be turned off", len(said) == 2)
    finally:
        render.info = real_info
    return ok


def check_live_failure():
    """rich allows one live display; a stale one used to need a restart."""

    class Stubborn:
        def __init__(self, *_args, **_kwargs):
            pass

        def start(self):
            raise RuntimeError("only one live display may be active at once")

    real_live, render.Live = render.Live, Stubborn
    try:
        ok = check("a live block that will not start gives up cleanly",
                   render._start_live(render.Text("x")) is None)
        view = render.MatchView(FakeContent())
        view.update(rows=[PlayerRow(puuid="p1", name="X#1", tier=17)], title="MATCH")
        ok &= check("the view says so instead of raising", view.degraded is True)
        view.close()
        ok &= check("and closing prints the table it could not refresh", view.degraded is False)
    finally:
        render.Live = real_live
    return ok


# -------------------------------------------------------------------- reveal


def check_reveal():
    """After the match, the people who hid get a name and a rank."""
    from valstats.app import App

    details = {
        "players": [
            {"subject": "me", "gameName": "Me", "tagLine": "EUW", "competitiveTier": 17},
            {"subject": "shy", "gameName": "Shy", "tagLine": "1234", "competitiveTier": 21},
            {"subject": "blank", "gameName": "", "tagLine": "", "competitiveTier": 0},
            {"gameName": "Nobody", "tagLine": "0"},
        ]
    }
    found = names(details)
    ok = check("a record that still names people is still read", found["shy"] == "Shy#1234")
    ok &= check("a record with no name in it is skipped", "blank" not in found)
    ok &= check("and a line with no puuid is not a player", len(found) == 2)
    ok &= check("an empty payload gives nothing back", names({}) == {})

    ranks = tiers(details)
    ok &= check("the rank comes out of the same record", ranks["shy"] == 21)
    ok &= check("unranked is not a rank worth recording", "blank" not in ranks)
    ok &= check("and a line with no puuid is still not a player", len(ranks) == 2)

    db = Encounters(path=":memory:")
    rows = [
        # mmr_read is what the live table sets for everyone it was allowed to
        # look up - which is everyone except the player who hid.
        PlayerRow(puuid="me", name="Me#EUW", is_self=True, tier=17, mmr_read=True),
        PlayerRow(puuid="shy", hidden=True),
    ]
    db.record("m9", rows)

    app = App.__new__(App)
    app.config = validate({}, warn=quiet)
    app.db = db
    app.content = FakeContent()
    asked_mmr = []

    def mmr_of(puuid, fresh=False):
        asked_mmr.append((puuid, fresh))
        return {
            "QueueSkills": {
                "competitive": {
                    "SeasonalInfoBySeasonID": {
                        ACT: {"CompetitiveTier": 20, "RankedRating": 55},
                        "older": {"CompetitiveTier": 23},
                    }
                }
            }
        }

    app.client = SimpleNamespace(puuid="me", names=lambda _p, fresh=False: {}, mmr=mmr_of)

    # The resolver holds the client it was built with; a new stand-in

    # needs a new one, the same way reconnect() rebuilds it.

    app.identity = None

    named, ranked, looked_up = app._reveal(rows, details, "m9")
    ok &= check("the hidden player is named once the match is over", rows[1].name == "Shy#1234")
    ok &= check("and stops being hidden", rows[1].hidden is False)
    ok &= check("the scoreboard can mark who that was", rows[1].revealed is True)
    ok &= check("you were never hidden from yourself", rows[0].revealed is False)
    ok &= check("the caption counts them", named == 1)
    ok &= check("their rank is looked up once they have a name again", rows[1].tier == 20)
    ok &= check("with the RR the match record does not carry", rows[1].rr == 55)
    ok &= check("and the act peak it does not carry either", rows[1].peak_tier == 23)
    ok &= check("the lookup is asked about them", asked_mmr == [("shy", True)])
    ok &= check("and not about anyone the lobby already read", ("me", True) not in asked_mmr)
    ok &= check("the caption counts the lookups", looked_up == 1)
    ok &= check("the record only fills what is left", ranked == 0)
    ok &= check("a rank we already had is left alone", rows[0].rank_revealed is False)
    ok &= check("the local memory learns the name too", db.find("Shy")[0]["puuid"] == "shy")
    ok &= check("and the rank they played it at", db.last_ranks(["shy"])["shy"]["tier"] == 21)
    ok &= check("your own rank is not an encounter", db.last_ranks(["me"]) == {})
    ok &= check("a name we already had is not rewritten", db.remember_names({"shy": "Shy#1234"}) == 0)
    ok &= check("nor is a stranger invented", db.remember_names({"ghost": "Who#1"}) == 0)

    cells = render._summary_cells(rows[1], own_team=None, content=FakeContent())
    ok &= check("the name reaches the scoreboard", cells["player"].plain == "Shy#1234")
    ok &= check("marked as one that was hidden", "cyan" in str(cells["player"].style))
    ok &= check("and so does the rank", cells["rank"].plain == "tier20")
    ok &= check("next to the RR", cells["rr"].plain == "55")
    ok &= check("and the peak", cells["peak"].plain == "tier23")
    ok &= check(
        "a scoreboard built without the content still prints",
        render._summary_cells(rows[1], own_team=None)["rank"].plain == "-",
    )

    # The band the live table read is still on the row when the match is over,
    # so the scoreboard carries it too - and nothing was asked for twice.
    rows[1].mmr_band = mmr.Band((21, 30), (22, 10), 84.0, 40.0, 14, True)
    rows[1].mmr_text = "tier21 - tier22"
    banded = render._summary_cells(rows[1], own_team=None, content=FakeContent())
    ok &= check("the hidden rating reaches the scoreboard", "tier21" in banded["mmr"].plain)
    ok &= check("with the arrow that says which way", banded["mmr"].plain.endswith("^"))
    ok &= check(
        "and drops out when nobody in the lobby has one",
        render._summary_cells(rows[0], own_team=None, content=FakeContent())["mmr"]
        is render.DASH,
    )

    pull = App._own_pull(
        SimpleNamespace(content=FakeContent()),
        [PlayerRow(puuid="them"), rows[1], PlayerRow(puuid="me", is_self=True)],
    )
    ok &= check("your own pull is only read off your own row", pull is None)
    rows[1].is_self = True
    pull = App._own_pull(SimpleNamespace(content=FakeContent()), [rows[1]])
    ok &= check("and says where the system is walking you", "tier21 - tier22" in pull)
    ok &= check("in the RR it was measured in", "+84 RR of pull" in pull)
    rows[1].is_self = False

    rr = SimpleNamespace(
        _rr_caption=lambda _m: "+18 RR -> Plat 2 60 RR",
        _own_pull=lambda _rows: "hidden rating tier21 - tier22 (+84 RR of pull)",
    )
    caption = App._caption(rr, "m9", [], 2, 1, 3)
    ok &= check("the caption still leads with your RR", caption.startswith("+18 RR"))
    ok &= check("then says where that RR is heading", "+84 RR of pull" in caption)
    ok &= check("and says why there are new names", "2 hidden players named" in caption)
    ok &= check("and where the new ranks came from", "1 rank read" in caption)
    ok &= check("and how many were looked up", "3 ranks and peaks looked up" in caption)
    ok &= check(
        "with nothing to say it says nothing",
        App._caption(
            SimpleNamespace(_rr_caption=lambda _m: None, _own_pull=lambda _rows: None),
            "m9",
            [],
            0,
            0,
            0,
        )
        is None,
    )

    # Riot stopped putting names in match-details; the name service still
    # answers about a player once the match with them is over.
    blank_record = {"players": [{"subject": "ghost", "competitiveTier": 24}]}
    late = [PlayerRow(puuid="ghost", hidden=True)]
    db.record("m10", late)
    asked = []

    def name_service(puuids, fresh=False):
        asked.append((tuple(puuids), fresh))
        return {"ghost": "Ghost#GG"}

    # An MMR lookup that answers nothing leaves the record as the only source
    # of a rank, which is the whole reason it is still read.
    app.client = SimpleNamespace(puuid="me", names=name_service, mmr=lambda _p, fresh=False: None)
    # The resolver holds the client it was built with; a new stand-in
    # needs a new one, the same way reconnect() rebuilds it.
    app.identity = None
    named, ranked, looked_up = app._reveal(late, blank_record, "m10")
    ok &= check("a record with no names left in it is not the end of it", named == 1)
    ok &= check("the name service is asked instead", late[0].name == "Ghost#GG")
    ok &= check("about the players we could not name", asked[0][0] == ("ghost",))
    ok &= check("and not from the blank we cached mid-match", asked[0][1] is True)
    ok &= check("an MMR lookup that says nothing counts for nothing", looked_up == 0)
    ok &= check("and the rank falls back to the record", late[0].tier == 24)
    ok &= check("which the caption then credits", ranked == 1)

    def refuses(_puuids, fresh=False):
        raise requests.ConnectionError("name service is down")

    app.client = SimpleNamespace(puuid="me", names=refuses, mmr=refuses)

    # The resolver holds the client it was built with; a new stand-in

    # needs a new one, the same way reconnect() rebuilds it.

    app.identity = None
    unnamed = [PlayerRow(puuid="nobody", hidden=True)]
    named, _ranked, looked_up = app._reveal(unnamed, blank_record, "m10")
    ok &= check("a name service that will not answer is not a crash", named == 0)
    ok &= check("nor is an MMR endpoint that will not either", looked_up == 0)

    quiet_app = App.__new__(App)
    quiet_app.config = validate({"reveal_after_match": False}, warn=quiet)
    quiet_app.db = db
    quiet_app.client = SimpleNamespace(puuid="me", names=name_service, mmr=refuses)
    still_hidden = [PlayerRow(puuid="shy2", hidden=True)]
    ok &= check(
        "turning it off leaves the match record unread",
        quiet_app._reveal(still_hidden, details, "m9") == (0, 0, 0),
    )
    ok &= check("and the player hidden", still_hidden[0].hidden is True)
    db.close()
    return ok


def check_remembered_ranks():
    """Someone who hides has no rank in the lobby - but we have met them before."""
    from valstats.app import App

    db = Encounters(path=":memory:")
    db.store_rank_snapshots("m1", {"shy": 21, "gone": 0})
    ok = check("a rank from a match record is remembered", db.last_ranks(["shy"])["shy"]["tier"] == 21)
    ok &= check("an unranked line is not a rank", db.last_ranks(["gone"]) == {})
    ok &= check("nor is a player we never met", db.last_ranks(["stranger"]) == {})

    # A live snapshot carries RR as well, so the record must not overwrite it.
    db.record("m2", [PlayerRow(puuid="shy", tier=22, rr=40)])
    db.store_rank_snapshots("m2", {"shy": 19})
    latest = db.last_ranks(["shy"])["shy"]
    ok &= check("the newest snapshot wins", latest["tier"] == 22)
    ok &= check("and the live one is not written over", latest["rr"] == 40)

    app = App.__new__(App)
    app.db = db
    rows = [
        PlayerRow(puuid="shy", hidden=True),
        PlayerRow(puuid="seen", tier=17),
        PlayerRow(puuid="stranger", hidden=True),
    ]
    app._remember_ranks(rows, asked={"seen"})
    ok &= check("the hidden player gets the rank we last saw", rows[0].tier == 22)
    ok &= check("dated, so the table can say so", bool(rows[0].tier_seen))
    ok &= check("a rank read for this lobby is left alone", rows[1].tier_seen == "")
    ok &= check("and someone we have never met stays a dash", rows[2].tier == 0)

    content = FakeContent()
    cells = render._row_cells(rows[0], content, own_team=None, show_perf=False)
    ok &= check("the table shows it", cells["rank"].plain == "~tier22")
    ok &= check("marked as not looked up today", "dim" in str(cells["rank"].style))
    ok &= check("and the name is still not shown", cells["player"].plain == "[hidden]")
    ok &= check(
        "a hidden player we know nothing about shows no rank",
        render._row_cells(rows[2], content, None, False)["rank"].plain == "-",
    )

    # The two empty Rank cells that are not the same answer: one is "nobody
    # has told us", the other is "their own matches say they never ranked".
    rows[2].unranked = True
    ok &= check(
        "but one whose matches say they never ranked is named as such",
        render._row_cells(rows[2], content, None, False)["rank"].plain == "tier0",
    )
    rows[2].unranked = False

    # And the form columns reach a hidden row now, because they were read out
    # of finished matches rather than out of a lookup Incognito closes.
    rows[0].apply_performance(
        {"acs": 231.0, "hs": 24.0, "kd": 1.2, "kast": 71.0, "dd": 12.0,
         "rating": 540, "matches": 5, "rounds": 100}
    )
    formed = render._row_cells(rows[0], content, own_team=None, show_perf=True)
    ok &= check("a hidden row now carries its form numbers", formed["acs"].plain == "231")
    ok &= check("and its score", formed["score"].plain == "540")
    ok &= check("while the name stays hidden", formed["player"].plain == "[hidden]")
    ok &= check(
        "and what only a lookup could say stays blank",
        formed["rr"].plain == "-" and formed["peak"].plain == "-",
    )

    said = []
    app.view = SimpleNamespace(status="", update=lambda **kw: said.append(kw.get("status")))
    app._note_remembered(rows)
    ok &= check("and the line under the table explains the mark", "last rank" in said[0])

    # Matches cached before ranks were read are offered up for one re-read -
    # and a match that turned out to hold no ranks at all must not come back
    # every time.
    db.store_match_perf("m3", {"p": dict(BLANK, rounds=20)})
    ok &= check("a cached match with no ranks is worth a re-read", "m3" in db.matches_missing_ranks())
    db.store_rank_snapshots("m3", {})
    ok &= check("but only the once", "m3" not in db.matches_missing_ranks())
    ok &= check("and one that had ranks is not offered either", "m1" not in db.matches_missing_ranks())
    db.close()
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

    # The band is the one column a rank does not repeat - where they stand is
    # the Rank column's job, where they are being carried is only here - so a
    # narrow window gives up the peak, the winrate and the RR before it.
    banded = PlayerRow(puuid="p5", tier=17, rr=40)
    banded.mmr_band = mmr.Band((17, 10), (18, 20), 60.0, 30.0, 12, True)
    banded.mmr_text = "tier17 - tier18"
    wanted = render._wanted(False, False, show_mmr=True)
    ok &= check("the lobby can carry the band at all", "mmr" in wanted)
    _kept, gone = render._fit(
        [render._row_cells(banded, content, "Blue", show_perf=False)], wanted, 40
    )
    ok &= check("the peak goes before the band", gone.index("Peak") < gone.index("Hidden MMR"))
    ok &= check("and so does the RR", gone.index("RR") < gone.index("Hidden MMR"))

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

    # The peak column: "hidden" is the player's own badge setting, and nothing
    # else. Turning the column off takes it away instead of writing that word
    # into every row, and a peak nobody looked up is a dash.
    shy = PlayerRow(puuid="p3", tier=17, peak_tier=21, peak_hidden=True)
    unread = PlayerRow(puuid="p4", tier=17)
    ok &= check("a hidden act badge says so", render._peak_text(content, shy).plain == "hidden")
    ok &= check("a peak nobody read is a dash", render._peak_text(content, unread).plain == "-")
    ok &= check(
        "turning the column off drops it from the lobby",
        "peak" not in render._wanted(True, True, show_peak=False),
    )
    ok &= check("and leaves it there when it is on", "peak" in render._wanted(True, True))
    def headers(peak, rows=(row, shy), width=200):
        # A fixed width, because the scoreboard drops columns to fit the window
        # and the window here is whatever the terminal running the tests is.
        was, render.console.width = render.console.width, width
        try:
            table = render.build_summary_table(
                list(rows), "t", own_team="Blue", content=content, show_peak=peak
            )
        finally:
            render.console.width = was
        return [column.header for column in table.columns]
    ok &= check("the scoreboard drops it too", "Peak" not in headers(False))
    ok &= check("and keeps it when asked to", "Peak" in headers(True))
    ok &= check(
        "a scoreboard where nobody has one leaves it out anyway",
        "Peak" not in headers(True, rows=(row,)),
    )
    ok &= check("and a narrow one gives it up first", "Peak" not in headers(True, width=60))
    return ok


# -------------------------------------------------------------------- lookup


def check_lookup():
    """Finding a player by a half-typed name, and the match behind the answer."""
    from valstats import lookup

    ok = check("an exact name is no distance at all", lookup.distance("jett", "jett") == 0)
    ok &= check("one wrong key is one edit", lookup.distance("jett", "jctt") == 1)
    ok &= check("a missing letter too", lookup.distance("jett", "jtt") == 1)
    ok &= check("two of them are two", lookup.distance("jett", "jctf") == 2)
    ok &= check("further than that is not measured", lookup.distance("jett", "sova") == 3)
    ok &= check(
        "and a length that cannot possibly fit is not even tried",
        lookup.distance("jett", "jettjettjett") == 3,
    )

    people = [
        {"puuid": "a", "name": "Ascent#EUW", "times": 2, "first_seen": "", "last_seen": ""},
        {"puuid": "b", "name": "Astra#1", "times": 9, "first_seen": "", "last_seen": ""},
        {"puuid": "c", "name": "St1xx-onion#RU1", "times": 4, "first_seen": "", "last_seen": ""},
        {"puuid": "d", "name": "nobody#0", "times": 1, "first_seen": "", "last_seen": ""},
        {"puuid": "e", "name": "", "times": 7, "first_seen": "", "last_seen": ""},
    ]
    named = lambda query, **kw: [p["name"] for p in lookup.suggest(people, query, **kw)]  # noqa: E731

    ok &= check("one letter offers everyone it starts", named("a") == ["Astra#1", "Ascent#EUW"])
    ok &= check("the one met most first", named("as")[0] == "Astra#1")
    ok &= check("an exact name wins over a longer one that starts the same", named("ascent#euw")[0] == "Ascent#EUW")
    ok &= check("case is not part of a name here", named("ASTRA") == ["Astra#1"])
    ok &= check("a name can be found by its middle", "St1xx-onion#RU1" in named("onion"))
    ok &= check("a slip of one key still finds it", named("st1x")[0] == "St1xx-onion#RU1")
    ok &= check("and of two", named("stixy")[0] == "St1xx-onion#RU1")
    ok &= check("three is a different player", named("sdfgh") == [])
    ok &= check(
        "at one or two letters nothing is guessed, only started",
        named("zz") == [] and named("z") == [],
    )
    ok &= check("nobody without a name is ever offered", "" not in named(""))
    ok &= check("an empty query offers the people you meet most", named("")[0] == "Astra#1")
    ok &= check("and never more than asked for", len(named("", limit=2)) == 2)

    # The timestamps in the database are UTC; what the table shows is the local
    # clock, because that is the evening you are trying to remember.
    utc = datetime(2026, 9, 9, 18, 44, 5, tzinfo=timezone.utc)
    shown = lookup._when("2026-09-09T18:44:05+00:00")
    ok &= check("a stored time is shown as local time", shown == utc.astimezone().strftime("%Y-%m-%d %H:%M"))
    ok &= check(
        "a timestamp with no zone on it is read as UTC",
        lookup._when("2026-09-09T18:44:05") == shown,
    )
    ok &= check(
        "seconds are there when the match itself is being dated",
        lookup._when("2026-09-09T18:44:05+00:00", seconds=True).endswith(":05"),
    )
    ok &= check("and nothing stored is not a crash", lookup._when(None) == "?")

    db = Encounters(path=":memory:")
    db.record("m1", [
        PlayerRow(puuid="me", name="Me#EUW", is_self=True),
        PlayerRow(puuid="foe", name="Foe#1"),
        PlayerRow(puuid="shy", hidden=True),
    ])
    db.store_match_perf("m1", {
        "me": dict(BLANK, rounds=20, kills=15, deaths=10, assists=4, score=5000, team="Blue", won=1),
        "foe": dict(BLANK, rounds=20, kills=12, deaths=12, assists=3, score=4000, team="Red"),
        "shy": dict(BLANK, rounds=20, kills=9, deaths=16, assists=2, score=3000, team="Red"),
    })
    db.store_match_meta("m1", {"map_id": ASCENT, "queue": "competitive", "started_at": "2026-09-09T18:44:05+00:00"})
    db.store_rank_snapshots("m1", {"shy": 21})

    offered = [p["name"] for p in db.named_players()]
    ok &= check("the completer is given the named players", offered == ["Foe#1"])
    ok &= check("and not the ones we never named", "shy" not in offered)
    ok &= check("you are not somebody you ran into", "Me#EUW" not in offered)

    info = db.match_info("m1")
    ok &= check("a match knows when it started", info["ts"] == "2026-09-09T18:44:05+00:00")
    ok &= check("and where", info["map_id"] == ASCENT)
    ok &= check("and its local number", info["num"] == 1)
    ok &= check("a match we have never seen is not invented", db.match_info("m404") is None)

    board = {row["puuid"]: row for row in db.match_lines("m1")}
    ok &= check("the scoreboard comes back whole", len(board) == 3)
    ok &= check("with the numbers", board["shy"]["kills"] == 9)
    ok &= check("the side", board["me"]["team"] == "Blue")
    ok &= check("the rank they played it at", board["shy"]["tier"] == 21)
    ok &= check("and the name where we have one", board["foe"]["name"] == "Foe#1")
    ok &= check("a hidden player is still on it, just nameless", not board["shy"]["name"])
    ok &= check("your own line has no name to show", not board["me"]["name"])
    ok &= check("so the scoreboard calls it what it is", lookup._who(board["me"], "me") == "you")
    ok &= check(
        "and a stranger by as much of their puuid as fits",
        lookup._who(board["shy"], "me").startswith("shy"),
    )
    db.close()
    return ok


# --------------------------------------------------------------------- picks


def _line(agent, **kwargs):
    """One cached per-match line for a given agent."""
    return dict(BLANK, agent=agent, rounds=20, score=5000, kast=15, **kwargs)


def _names(picks):
    return [p.name for p in picks]


def check_share():
    """What leaves the machine when sharing is on - and what must not."""
    from valstats import share

    salt = "test-salt"
    ok = check(
        "a puuid is replaced by a stable 32-character hash",
        share.token("abc", salt) == share.token("abc", salt) and len(share.token("abc", salt)) == 32,
    )
    ok &= check("different players hash differently", share.token("a", salt) != share.token("b", salt))
    ok &= check(
        "a different salt is a different pool",
        share.token("a", salt) != share.token("a", "other-salt"),
    )
    ok &= check("nothing in, nothing out", share.token("", salt) == "" and share.token(None, salt) == "")
    ok &= check(
        "the hash cannot be read back as the puuid",
        "abc" not in share.token("abc", salt),
    )

    db = Encounters(path=":memory:")
    lines = {}
    for index in range(10):
        lines[f"player{index}"] = dict(
            BLANK,
            rounds=24,
            kills=20,
            score=6000,
            team="Blue" if index < 5 else "Red",
            party="party-one" if index < 2 else f"solo{index}",
            agent="jett",
            won=1 if index < 5 else 0,
        )
    db.store_match_perf("real-match-id", lines)
    db.store_match_meta(
        "real-match-id",
        {
            "map_id": "/game/maps/ascent/ascent",
            "queue": "competitive",
            "started_at": "2026-09-14T20:13:07+00:00",
            "length": 2145,
            "score": "Blue:13,Red:9",
        },
    )

    waiting = db.unshared(10)
    ok &= check("a parsed match with a time is offered", waiting == ["real-match-id"])

    body = share.payload(db, waiting, salt)
    ok &= check("one record per match", len(body) == 1)
    record = body[0]
    ok &= check("all ten lines go or none do", len(record["p"]) == 10)
    ok &= check("the exact start time is carried", record["t"] == "2026-09-14T20:13:07+00:00")
    ok &= check("so is the final score", record["sc"] == "Blue:13,Red:9")
    ok &= check("and the length", record["len"] == 2145)

    blob = json.dumps(record)
    ok &= check("the real match id never goes out", "real-match-id" not in blob)
    ok &= check("nor does a raw puuid", "player0" not in blob and "player9" not in blob)
    ok &= check("nor a raw party id", "party-one" not in blob)
    ok &= check(
        "two players in one party share one hash",
        record["p"][0]["g"] or record["p"][1]["g"],
    )
    parties = {line["g"] for line in record["p"]}
    ok &= check("a five-way split of parties stays a split", len(parties) == 9)

    ok &= check("no name field is ever built", "name" not in blob and "#" not in blob)

    # The ledger: once sent, never offered again.
    db.mark_shared(waiting)
    ok &= check("a shared match is not offered twice", db.unshared(10) == [])
    counts = db.share_counts()
    ok &= check("the counters add up", counts["shared"] == 1 and counts["waiting"] == 0)
    db.forget_shared()
    ok &= check("and the ledger can be cleared", db.unshared(10) == ["real-match-id"])

    # A match missing a player is not a match anybody downstream can trust.
    short = {f"p{i}": dict(BLANK, rounds=24, team="Blue") for i in range(9)}
    db.store_match_perf("short-match", short)
    db.store_match_meta(
        "short-match",
        {"map_id": "m", "queue": "competitive", "started_at": "2026-09-14T21:00:00+00:00"},
    )
    ok &= check(
        "a nine-player match is never pooled",
        len(share.payload(db, ["short-match"], salt)) == 0,
    )

    # A match with no start time cannot be placed and is not offered at all.
    db.store_match_perf("timeless", {f"q{i}": dict(BLANK, rounds=24) for i in range(10)})
    ok &= check("a match with no time is not offered", "timeless" not in db.unshared(10))

    # Sharing off means nothing goes, whatever is queued.
    sent = share.push({"share_stats": False, "share_endpoint": "https://x"}, db)
    ok &= check("sharing off sends nothing", sent == 0)
    sent = share.push({"share_stats": True, "share_endpoint": ""}, db)
    ok &= check("no endpoint sends nothing", sent == 0)

    db.conn.close()
    return ok


def check_update():
    """Version comparison, and the rule that local data is never written."""
    import io as _io
    import shutil as _shutil
    import zipfile as _zipfile
    from pathlib import Path as _Path

    from valstats import update

    ok = check("a plain version parses", update.parts("1.2.3") == (1, 2, 3))
    ok &= check("a v prefix is tolerated", update.parts("v2.0") == (2, 0, 0))
    ok &= check("and a release suffix", update.parts("1.4.0-beta2") == (1, 4, 0))
    ok &= check("nonsense is not a version", update.parts("") == (0, 0, 0))
    ok &= check("a higher version is newer", update.newer("1.1.0", "1.0.0"))
    ok &= check("double digits sort as numbers", update.newer("1.10.0", "1.9.0"))
    ok &= check("the same version is not newer", not update.newer("1.0.0", "1.0.0"))
    ok &= check("nor is an older one", not update.newer("0.9.9", "1.0.0"))

    root = _Path(tempfile.mkdtemp(prefix="valstats-upd-"))
    try:
        (root / "valstats").mkdir()
        (root / "cache").mkdir()
        (root / "valstats" / "__init__.py").write_text('__version__ = "1.0.0"')
        (root / "valstats" / "app.py").write_text("OLD")
        (root / "run.bat").write_text("OLD")
        (root / "requirements.txt").write_text("requests")
        (root / "config.json").write_text('{"riot_api_key": "secret"}')
        (root / "encounters.db").write_bytes(b"SQLite format 3")
        (root / "cache" / "agents.json").write_text("{}")

        # An archive that carries the local files too, which it must not install.
        buf = _io.BytesIO()
        with _zipfile.ZipFile(buf, "w") as archive:
            pre = "owner-repo-abc123/"
            archive.writestr(pre + "valstats/__init__.py", '__version__ = "1.1.0"')
            archive.writestr(pre + "valstats/app.py", "NEW")
            archive.writestr(pre + "run.bat", "NEW")
            archive.writestr(pre + "requirements.txt", "requests")
            archive.writestr(pre + "config.json", '{"riot_api_key": "STOLEN"}')
            archive.writestr(pre + "encounters.db", "WIPED")
            archive.writestr(pre + "cache/agents.json", "WIPED")

        staged = update.unpack(_zipfile.ZipFile(_io.BytesIO(buf.getvalue())), root / "_s")
        ok &= check("the github wrapper folder is dropped", (staged / "run.bat").is_file())
        ok &= check("the archive unpacks whole", update.sane(staged))

        wanted = {path.as_posix() for path in update.plan(staged)}
        ok &= check("code is installable", "valstats/app.py" in wanted)
        ok &= check("config.json is never installable", "config.json" not in wanted)
        ok &= check("nor is the database", "encounters.db" not in wanted)
        ok &= check("nor anything in the cache", not any(w.startswith("cache/") for w in wanted))

        real_fetch = update.fetch
        update.fetch = lambda url: _zipfile.ZipFile(_io.BytesIO(buf.getvalue()))
        try:
            backup = update.install({"url": "x", "version": "1.1.0"}, root=root, say=lambda m: None)
        finally:
            update.fetch = real_fetch

        ok &= check("the code was replaced", (root / "valstats" / "app.py").read_text() == "NEW")
        ok &= check(
            "the config survived untouched",
            (root / "config.json").read_text() == '{"riot_api_key": "secret"}',
        )
        ok &= check(
            "and so did the database",
            (root / "encounters.db").read_bytes() == b"SQLite format 3",
        )
        ok &= check("and the content cache", (root / "cache" / "agents.json").read_text() == "{}")
        ok &= check(
            "the previous code is in the backup",
            (backup / "valstats" / "app.py").read_text() == "OLD",
        )

        # An archive that tries to climb out of the folder is refused outright.
        evil = _io.BytesIO()
        with _zipfile.ZipFile(evil, "w") as archive:
            archive.writestr("pkg/valstats/__init__.py", "x")
            archive.writestr("pkg/../../escaped.txt", "pwned")
        try:
            update.unpack(_zipfile.ZipFile(_io.BytesIO(evil.getvalue())), root / "_e")
            ok &= check("a path climbing out of the archive is refused", False)
        except update.Failed:
            ok &= check("a path climbing out of the archive is refused", True)

        # An archive that is not this program is refused before anything is written.
        bare = _io.BytesIO()
        with _zipfile.ZipFile(bare, "w") as archive:
            archive.writestr("x/readme.txt", "hello")
        try:
            update.sane(update.unpack(_zipfile.ZipFile(_io.BytesIO(bare.getvalue())), root / "_b"))
            ok &= check("something that is not this program is refused", False)
        except update.Failed:
            ok &= check("something that is not this program is refused", True)
    finally:
        _shutil.rmtree(root, ignore_errors=True)

    # No repository configured means the check does nothing at all.
    ok &= check(
        "with no update_repo nothing is checked",
        update.available({"update_repo": "", "check_updates": True}) is None,
    )
    ok &= check(
        "and turning it off turns it off",
        update.available({"update_repo": "a/b", "check_updates": False}) is None,
    )
    return ok


def check_party_source():
    """Naming a hidden player who is in your own party, and nobody else."""

    class Stub:
        """A client with a party of three and two chat lists behind it."""

        def __init__(self, party=("me", "mate", "shy"), fail=False):
            self.party = party
            self.fail = fail
            self.asked = []

        def own_party(self):
            if self.fail:
                raise ClientUnavailable("no game")
            return {"id": "p1", "members": list(self.party)} if self.party else None

        def local(self, method, path):
            self.asked.append(path)
            if path == "/chat/v5/participants":
                return {"participants": [
                    {"puuid": "shy", "game_name": "Shy", "game_tag": "1234"},
                ]}
            if path == "/chat/v4/friends":
                return {"friends": [
                    {"puuid": "mate", "game_name": "Mate", "game_tag": "EUW"},
                    {"puuid": "enemy", "game_name": "Enemy", "game_tag": "RU1"},
                ]}
            return {}

    stub = Stub()
    source = identity.PartyRoom(stub)
    found = source.lookup(["shy", "mate", "enemy"], {})
    ok = check("a hidden party member is named", found["shy"].name == "Shy#1234")
    ok &= check("from the chat room they are sitting in", found["shy"].source == "party")
    ok &= check("a party member on the friends list too", found["mate"].name == "Mate#EUW")
    ok &= check(
        "but somebody outside your party is never named here, even when known",
        "enemy" not in found,
    )

    # An enemy hiding is exactly the case this cannot and must not answer.
    ok &= check("asked only about outsiders, it says nothing", source.lookup(["enemy"], {}) == {})
    ok &= check("and does not even read the chat lists", stub.asked.count("/chat/v4/friends") == 1)

    # Solo queue and a shut game are both "no party", and neither is an error.
    ok &= check("solo queue names nobody", identity.PartyRoom(Stub(party=())).lookup(["shy"], {}) == {})
    ok &= check(
        "a game that is not running is not a crash",
        identity.PartyRoom(Stub(fail=True)).lookup(["shy"], {}) == {},
    )
    ok &= check(
        "and with no client there is nothing to ask",
        identity.PartyRoom(None).lookup(["shy"], {}) == {},
    )

    ok &= check("the source is offered in config", "party" in identity.KNOWN_SOURCES)
    ok &= check(
        "and sits behind the two free ones",
        identity.DEFAULT_SOURCES.index("party") > identity.DEFAULT_SOURCES.index("chat"),
    )
    return ok


def check_own_party():
    """Your own party, which Riot names outright, needs no inference at all."""
    from valstats.party import apply as stamp
    from valstats.party import describe, detect

    lobby = [PlayerRow(puuid=p, team="Blue") for p in ("me", "a", "b", "c", "d")]
    lobby += [PlayerRow(puuid=p, team="Red") for p in ("e1", "e2")]

    # A cold cache: no shared matches with anybody, ever. The old path finds
    # nothing here, which is exactly the case this is for.
    found = detect(lobby, {}, roster=["me", "a", "b"])
    ok = check("a roster is a group with no cache at all", len(found) == 1)
    ok &= check("and holds exactly the roster", found[0].members == ["a", "b", "me"])
    ok &= check("and is confirmed", found[0].confirmed is True)
    ok &= check("so it is not marked thin", found[0].strong is True and found[0].solid is True)

    stamp(lobby, found)
    marks = {row.puuid: row.party for row in lobby}
    ok &= check("members are starred, not questioned", marks["me"] == "A*" == marks["a"])
    ok &= check("a teammate outside the party is left alone", marks["d"] == "")
    ok &= check("and so is the enemy", marks["e1"] == "")
    ok &= check(
        "the line stops hedging for a certainty",
        describe(found).startswith("queued together"),
    )
    ok &= check("and does not print a shared count", "shared" not in describe(found))

    # Solo queue: Riot still reports a party, of one. That is not a group.
    ok &= check("a party of one is not a party", detect(lobby, {}, roster=["me"]) == [])
    ok &= check("nor is an empty roster", detect(lobby, {}, roster=[]) == [])

    # A roster naming somebody who is not in this lobby must not invent a row.
    stray = detect(lobby, {}, roster=["me", "a", "ghost"])
    ok &= check("a roster member not in the lobby is ignored", stray[0].members == ["a", "me"])

    # Inference and certainty in one lobby: both are shown, letters differ.
    both = detect(
        lobby,
        {("e1", "e2"): ["2026-05-01T10:00:00+00:00", "2026-06-04T10:00:00+00:00"]},
        roster=["me", "a"],
        own_team="Blue",
    )
    ok &= check("both kinds of group are found", len(both) == 2)
    stamp(lobby, both)
    marks = {row.puuid: row.party for row in lobby}
    ok &= check("yours is starred", marks["me"].endswith("*"))
    ok &= check("theirs is not", marks["e1"] and not marks["e1"].endswith("*"))
    ok &= check(
        "and a mixed line goes back to hedging",
        describe(both).startswith("likely queued together"),
    )
    return ok


def check_party_confirmed():
    """Riot's own party id, when a pooled match carries it, beats the guess."""
    from valstats.party import detect

    lobby = [
        PlayerRow(puuid="a", team="Blue"),
        PlayerRow(puuid="b", team="Blue"),
        PlayerRow(puuid="c", team="Blue"),
    ]
    # One shared match is below the threshold and on its own proves nothing.
    thin = {("a", "b"): ["2026-05-01T10:00:00+00:00"]}
    ok = check("one shared match is still not a party", detect(lobby, thin) == [])

    # The same pair, with Riot's party id agreeing once.
    confirmed = {("a", "b"): ["2026-05-01T10:00:00+00:00"]}
    found = detect(lobby, thin, confirmed=confirmed)
    ok &= check("a confirmed pair is a party on one match", len(found) == 1)
    ok &= check("and it is not marked as thin", found[0].strong is True)
    ok &= check("and it says it was confirmed", found[0].confirmed is True)
    ok &= check("the third player stays out of it", found[0].members == ["a", "b"])

    # Confirmation alone, with no co-occurrence evidence at all, still counts.
    only = detect(lobby, {}, confirmed={("b", "c"): ["2026-06-02T20:00:00+00:00"]})
    ok &= check("confirmation needs no other evidence", len(only) == 1)
    ok &= check("and finds the right pair", only[0].members == ["b", "c"])

    # A guessed group is still a guess, and must not claim confirmation.
    guessed = detect(
        lobby,
        {("a", "b"): ["2026-05-01T10:00:00+00:00", "2026-06-04T10:00:00+00:00"]},
    )
    ok &= check("an inferred party is found", len(guessed) == 1)
    ok &= check("but does not claim to be confirmed", guessed[0].confirmed is False)
    return ok


def check_picks():
    content = FakeContent()
    ok = True

    # An empty team: nothing but the composition to go on.
    empty = recommend(content, [], [], limit=6, per_role=0)
    ok &= check("with nothing picked the controller comes first", empty[0].name == "Omen")
    ok &= check("the advice explains itself", "no controller" in empty[0].reason)
    ok &= check(
        "an untried agent is admitted as untried", "nothing cached" in empty[0].reason
    )

    # Once someone smokes, the hole moves to the other roles.
    with_smokes = recommend(content, [OMEN], [], limit=6, per_role=0)
    ok &= check("a taken agent is never suggested", "Omen" not in _names(with_smokes))
    ok &= check("with a controller up the initiator leads", with_smokes[0].name == "Sova")

    filled = recommend(content, [OMEN, SOVA, SAGE], [], limit=3, per_role=0)
    ok &= check("a duelist leads once the utility is covered", filled[0].role == "Duelist")

    # Two sentinels are worth less than the first of a missing role.
    both = {p.name: p.score for p in recommend(content, [SAGE], [], limit=6, per_role=0)}
    ok &= check("the second sentinel is worth less than the first", both["Killjoy"] < both["Omen"])

    # Your own record breaks the tie between two agents of the same role.
    mine = [_line(REYNA, won=1) for _ in range(8)] + [_line(JETT, kills=0, deaths=200)]
    duelists = recommend(content, [OMEN, SOVA, SAGE], mine, limit=6, per_role=0)
    ok &= check(
        "the duelist you actually play wins the tie",
        _names(duelists).index("Reyna") < _names(duelists).index("Jett"),
    )
    ok &= check("the reason quotes your record", "over 8" in duelists[0].reason)

    # Owning an agent is a hard filter, not a nudge.
    owned = recommend(content, [], [], pool={JETT, SAGE}, limit=6, per_role=0)
    ok &= check("agents you do not own are left out", set(_names(owned)) == {"Jett", "Sage"})

    # Map record only speaks up once there is enough of it.
    thin = [_line(KILLJOY, won=0, map_id=ASCENT) for _ in range(2)]
    thick = [_line(KILLJOY, won=1, map_id=ASCENT) for _ in range(6)]
    quiet_map = recommend(content, [], thin, map_id=ASCENT, limit=6, per_role=0)[0]
    ok &= check("two matches on a map say nothing", "here over" not in str(quiet_map.reason))
    loud = [p for p in recommend(content, [], thick, map_id=ASCENT, limit=6, per_role=0)
            if p.name == "Killjoy"]
    ok &= check("a real map record is quoted", "here over 6" in loud[0].reason)

    elsewhere = [p for p in recommend(content, [], thick, map_id=HAVEN, limit=6, per_role=0)
                 if p.name == "Killjoy"]
    ok &= check(
        "a record on another map does not count here", "here over" not in elsewhere[0].reason
    )

    ok &= check(
        "only as many as asked for come back",
        len(recommend(content, [], [], limit=2, per_role=0)) == 2,
    )

    # The same lobby must not reshuffle between redraws.
    twice = [_names(recommend(content, [], [], limit=6, per_role=0)) for _ in range(2)]
    ok &= check("ties break the same way every time", twice[0] == twice[1])

    crowded = recommend(content, [JETT, REYNA, SOVA], [], limit=6, per_role=0)
    third = [p for p in crowded if p.role == "Duelist"]
    ok &= check("a third duelist is spelled out properly", "3rd duelist" in third[0].reason)

    # The block that actually reaches the screen.
    def drawn(picks, heading="Suggested picks"):
        panel = render.build_picks(picks, heading)
        return "".join(seg.text for seg in render.console.render(panel))

    text = drawn(recommend(content, [], [], limit=3))
    ok &= check("the panel names the agents", "Omen" in text and "Sova" in text)
    ok &= check("the panel shows the roles", "Controller" in text)
    ok &= check("the panel numbers the places", "1" in text and "3" in text)
    ok &= check("the heading is carried through", "Suggested picks" in text)
    ok &= check("an empty list is still safe to draw", "Nothing" in drawn([], "Nothing"))

    # Three suggestions have to be three different answers, not one repeated:
    # with an empty cache every controller scores the same, and a list of them
    # tells you nothing you did not already read on the first line.
    spread = recommend(content, [], [], limit=3)
    ok &= check("the default list holds one agent per role", len({p.role for p in spread}) == 3)
    ok &= check("the most-needed role still leads", spread[0].role == "Controller")
    two_each = recommend(content, [], [], limit=4, per_role=2)
    ok &= check(
        "the cap is what it says it is",
        max(sum(1 for p in two_each if p.role == r) for r in {p.role for p in two_each}) == 2,
    )

    rows = [
        PlayerRow(puuid="me", agent_id=JETT, is_self=True),
        PlayerRow(puuid="a1", agent_id=OMEN),
        PlayerRow(puuid="a2", agent_id=""),
    ]
    ok &= check("your own hover is not counted as taken", taken_agents(rows) == [OMEN])
    return ok


def check_by_agent():
    ok = True
    lines = [
        _line(REYNA, map_id=ASCENT, won=1),
        _line(REYNA, map_id=HAVEN, won=0),
        _line(JETT, map_id=ASCENT, won=1),
        dict(BLANK, rounds=20, score=5000),  # cached before agents were recorded
    ]
    pool = by_agent(lines)
    ok &= check("lines group by the agent they were played on", set(pool) == {REYNA, JETT})
    ok &= check("both Reyna matches are counted", pool[REYNA]["matches"] == 2)
    ok &= check("a line with no agent is dropped", len(pool) == 2)

    on_map = by_agent(lines, map_id=ASCENT)
    ok &= check("filtering by map keeps only that map", on_map[REYNA]["matches"] == 1)
    ok &= check("an unplayed map gives nothing back", by_agent(lines, map_id="nowhere") == {})
    return ok


def check_meta():
    ok = True
    details = {
        "matchInfo": {
            "matchId": "m1",
            "mapId": "/Game/Maps/Ascent/Ascent",
            "queueID": "competitive",
            "gameStartMillis": 1700000000000,
        }
    }
    parsed = meta(details)
    ok &= check(
        "the map is lower-cased for lookups", parsed["map_id"] == "/game/maps/ascent/ascent"
    )
    ok &= check("the queue is read", parsed["queue"] == "competitive")
    ok &= check("the start time becomes a timestamp", parsed["started_at"].startswith("2023-11-14"))
    ok &= check(
        "queueId spelled the other way still reads",
        meta({"matchInfo": {"queueId": "x"}})["queue"] == "x",
    )
    ok &= check("a payload with no match info does not explode", meta({})["map_id"] == "")
    ok &= check("a missing start time stays empty", meta({})["started_at"] is None)
    return ok



# --------------------------------------------------------------- hidden MMR


def _update(earned, match_id="m", **extra):
    """One competitiveupdates entry: an ordinary match unless told otherwise."""
    entry = {
        "MatchID": match_id,
        "RankedRatingEarned": earned,
        "RankedRatingPerformanceBonus": 0,
        "TierBeforeUpdate": 13,
        "TierAfterUpdate": 13,
        "RankedRatingBeforeUpdate": 50,
        "RankedRatingAfterUpdate": 50 + earned,
        "AFKPenalty": 0,
        "RRPenalty": 0,
        "RankedRatingRefundApplied": 0,
        "NewMapIncentiveRRForgiven": 0,
        "IsPlacementMatch": False,
        "WasDerankProtected": False,
    }
    entry.update(extra)
    return entry


def _run(gains, losses, **extra):
    updates = [_update(value, f"w{i}", **extra) for i, value in enumerate(gains)]
    updates += [_update(value, f"l{i}", **extra) for i, value in enumerate(losses)]
    return updates


def check_mmr(content):
    ok = True

    level = mmr.read(_run([20] * 4, [-20] * 4))
    band = mmr.estimate(13, 50, level)
    ok &= check("even RR reads as no drift", abs(level.drift) < 0.01)
    ok &= check("no drift means no gap", abs(band.gap) < 0.01)
    ok &= check("and the mark says level", mmr.mark(band) == "=")

    pushed = mmr.read(_run([26] * 5, [-14] * 5))
    up = mmr.estimate(13, 50, pushed)
    ok &= check("wins paying more reads as drift up", pushed.drift == 6)
    ok &= check("a drift of 6 is over a division", up.gap == 120)
    ok &= check("and is marked as such", mmr.mark(up) == "^^")
    ok &= check("the band lands above the rank", up.low[0] >= 14)

    pulled = mmr.estimate(13, 50, mmr.read(_run([15] * 5, [-25] * 5)))
    ok &= check("losses costing more reads as drift down", pulled.gap == -100)
    ok &= check("and is marked down", mmr.mark(pulled) == "v")

    # Every flag Riot sets is a reason not to read that match as an ordinary one.
    for flag, value, reason in (
        ("IsPlacementMatch", True, "placement"),
        ("WasDerankProtected", True, "derank protection"),
        ("AFKPenalty", 8, "afk penalty"),
        ("RRPenalty", 0.25, "party penalty"),
        ("RankedRatingRefundApplied", 12, "rr refunded"),
        ("TierAfterUpdate", 14, "changed tier"),
    ):
        got = mmr.skip_reason(_update(20, **{flag: value}))
        ok &= check(f"{reason} is not ordinary RR", got == reason)
    ok &= check("an outlier is dropped", mmr.skip_reason(_update(80)) == "outlier")
    ok &= check("a zero cannot be read", mmr.skip_reason(_update(0)) == "no movement")

    counted = mmr.read(_run([22] * 4, [-18] * 4) + [_update(20, "p", IsPlacementMatch=True)])
    ok &= check("skipped matches are counted, not hidden", counted.skipped == {"placement": 1})

    ok &= check("two wins is not a reading", mmr.read(_run([20, 20], [-20] * 4)) is None)
    ok &= check("nothing at all is not a reading", mmr.read([]) is None)

    # The performance bonus is paid for how they played, not for where the
    # system thinks they belong, so it must not read as the pull.
    bonus = mmr.read(
        [_update(30, f"w{i}", RankedRatingPerformanceBonus=10) for i in range(4)]
        + [_update(-20, f"l{i}") for i in range(4)]
    )
    ok &= check("the performance bonus comes off first", abs(bonus.drift) < 0.01)

    ok &= check(
        "Immortal gets the pull but no rank on it",
        mmr.estimate(24, 180, pushed).placed is False
        and mmr.estimate(24, 180, pushed).low is None
        and mmr.estimate(24, 180, pushed).gap == 120,
    )
    ok &= check(
        "and prints as a distance rather than as a division",
        mmr.describe(mmr.estimate(24, 180, pushed), content) == "+120 RR",
    )
    ok &= check(
        "an unplaced band is never called sure",
        not mmr.sure(mmr.estimate(24, 180, pushed)),
    )
    ok &= check("unranked gets none either", mmr.estimate(0, 0, pushed) is None)

    # A sample that agrees with itself is narrow; a scattered one is wide, and
    # the band has to say so rather than quoting the same three digits.
    tight = mmr.estimate(13, 50, mmr.read(_run([24] * 6, [-16] * 6)))
    loose = mmr.estimate(
        13, 50, mmr.read(_run([10, 38, 24, 12, 36, 24], [-30, -2, -16, -28, -4, -16]))
    )
    # Narrow, but never zero: twelve matches that all paid the same is a small
    # sample agreeing with itself, and MATCH_NOISE is what one match carries
    # whatever those twelve happened to land on.
    ok &= check(
        "a consistent sample gives a narrow band",
        0 < tight.spread < mmr.RR_PER_DIVISION / 4,
    )
    ok &= check("a scattered one gives a wide band", loose.spread > tight.spread + 40)
    ok &= check("both read the same drift", abs(tight.gap - loose.gap) < 0.01)
    ok &= check("and only the narrow one is called sure", mmr.sure(tight) and not mmr.sure(loose))

    ends = (content.tier(up.low[0])["name"], content.tier(up.high[0])["name"])
    ok &= check(
        "a band inside one division is named once",
        up.low[0] == up.high[0] and mmr.describe(up, content) == ends[0],
    )
    ok &= check(
        "a band across two is named at both ends",
        mmr.describe(loose, content)
        == f"{content.tier(loose.low[0])['name']} - {content.tier(loose.high[0])['name']}",
    )
    ok &= check("no reading explains itself", "not enough" in mmr.explain(None, None))
    ok &= check("a reading explains itself", "drift" in mmr.explain(pushed, up))
    ok &= check_mmr_depth()
    return ok


# The RR history as the cache hands it back: dated, newest first. Everything
# below needs the dates, because everything below is about which end of the
# sample a match came from.
MMR_EPOCH = 1_700_000_000_000


def _dated(earned, index, tier=13, rr=50, **extra):
    entry = _update(earned, f"m{index}", **extra)
    entry["TierBeforeUpdate"] = tier
    entry["TierAfterUpdate"] = extra.get("TierAfterUpdate", tier)
    entry["RankedRatingBeforeUpdate"] = rr
    entry["RankedRatingAfterUpdate"] = extra.get("RankedRatingAfterUpdate", rr + earned)
    entry["MatchStartTime"] = MMR_EPOCH - index * 3_600_000
    return entry


def _alternating(count, win, loss, start=0, **extra):
    """count matches, newest first, paying `win` and costing `loss` in turn."""
    return [
        _dated(win if index % 2 == 0 else loss, start + index, **extra)
        for index in range(count)
    ]


def check_mmr_depth():
    """The four things the estimate does that a mean of the deltas does not."""
    ok = True

    # A promotion that carried its overflow is ordinary evidence: the ranks
    # before and after are both in the payload, and their difference agrees
    # with what the match says it paid. Nothing was clipped, so nothing is
    # lost by reading it - and these are exactly the matches a climbing
    # player's estimate is made of.
    promotion = _dated(20, 1, tier=13, rr=95, TierAfterUpdate=14, RankedRatingAfterUpdate=15)
    ok &= check("a promotion that kept its overflow is still readable", not mmr.skip_reason(promotion))
    ok &= check("and is counted as recovered", mmr.recovered(promotion))
    demotion = _dated(-20, 2, tier=14, rr=10, TierAfterUpdate=13, RankedRatingAfterUpdate=80)
    ok &= check(
        "a demotion caught by the floor is still dropped",
        mmr.skip_reason(demotion) == "changed tier",
    )
    ok &= check(
        "the recovered ones are reported, not folded in quietly",
        mmr.read(_run([22] * 4, [-18] * 4) + [promotion]).recovered == 1,
    )

    # Recency. The gap this measures is the gap now, and the system has been
    # closing it all sample long, so the newer half has to weigh more. With no
    # dates on the entries nothing is assumed and the answer is the flat one.
    swung = _alternating(10, 26, -14) + _alternating(10, 14, -26, start=10)
    undated = [{key: value for key, value in entry.items() if key != "MatchStartTime"} for entry in swung]
    ok &= check("a sample that swung recently reads as the recent half", mmr.read(swung).drift > 1)
    ok &= check(
        "the same sample with no dates on it is read flat",
        abs(mmr.read(undated).drift) < 0.01,
    )

    # The trend: the same drift reads differently depending on whether it is
    # shrinking. Both halves need wins and losses of their own before this
    # answers at all.
    closing = mmr.read(_alternating(10, 22, -18) + _alternating(10, 28, -12, start=10))
    ok &= check("a gap that is shrinking says so", mmr.settling(closing) == "closing")
    ok &= check("and the two halves are kept", closing.trend == (8.0, 2.0))
    ok &= check(
        "a sample too short to halve has no trend",
        mmr.read(_run([24] * 3, [-16] * 3)).trend is None,
    )

    # The constant, measured rather than assumed. This history is a player
    # whose drift fell from +8 to +4 while their rank climbed 80 RR, which by
    # the servo model is a convergence of exactly 20 matches.
    history = _alternating(8, 24, -16, rr=80) + _alternating(8, 28, -12, start=8, rr=0)
    measured = mmr.calibrate([history] * mmr.MIN_CALIBRATION_PLAYERS)
    ok &= check("the convergence constant can be measured from a cache", measured is not None)
    ok &= check("and comes back as the constant the history was built with", abs(measured.matches - 20) < 0.5)
    ok &= check(
        "one player short of the minimum is no answer at all",
        mmr.calibrate([history] * (mmr.MIN_CALIBRATION_PLAYERS - 1)) is None,
    )
    ok &= check(
        "a history whose drift did not move does not vote",
        mmr.calibrate([_alternating(16, 24, -16)] * 10) is None,
    )
    # A measured constant scales the answer; a silly one is refused outright
    # rather than quietly halving everybody's gap.
    reading = mmr.read(_run([26] * 5, [-14] * 5))
    ok &= check(
        "a measured constant is what the gap is scaled by",
        mmr.estimate(13, 50, reading, mmr.Calibration(30.0, 40, 2.0)).gap == 180,
    )
    ok &= check(
        "a constant outside the rails falls back to the default",
        mmr.estimate(13, 50, reading, mmr.Calibration(400.0, 40, 2.0)).gap == 120,
    )
    return ok


# ----------------------------------------------------------------- the sweep


class _SweepClient:
    """A Riot client with nothing to say, so a sweep costs no time."""

    def __init__(self, on_player=None):
        self.asked = []
        self.on_player = on_player or (lambda puuid: None)

    def match_history(self, puuid, count, queue, start=0):
        return {"History": []}

    def competitive_updates(self, puuid, count):
        # The last call a sweep makes about one player, so it is where a test
        # can say "and now the match started".
        self.asked.append(puuid)
        self.on_player(puuid)
        return []


class _SweepDb:
    def __init__(self, cached=None):
        self.cached = cached or {}

    def matches_missing_conduct(self, ids):
        return set()

    def is_parsed(self, match_id):
        return True

    def store_rr_updates(self, puuid, matches):
        pass

    def cached_counts(self, puuids):
        return {puuid: self.cached.get(puuid, 0) for puuid in puuids}


def _boom():
    raise RuntimeError("the presence read failed")


def check_hidden_rank():
    """A stranger behind [hidden] used to be a dash in the Rank column.

    The lookup is the one request the live table declines to make about
    somebody in Incognito, and the local memory only helps if they have stood
    in one of our lobbies before. But the record of a finished match carries
    competitiveTier for everybody who played it, hiding or not - the same
    source the post-match reveal uses, and not something Incognito covers.

    The rule this pins down is "exactly as far as it has to": their matches
    newest first, stopping at the first one that records a rank.
    """
    from valstats.app import App

    db = Encounters(path=":memory:")
    try:
        asked = {"history": [], "details": []}

        def match_history(puuid, count=5, queue="competitive", start=0):
            asked["history"].append((puuid, count, queue))
            return {"History": [{"MatchID": m} for m in ("new", "older", "oldest")]}

        def match_details(match_id):
            asked["details"].append(match_id)
            info = {"matchInfo": {"gameStartMillis": 1757000000000, "queueID": "competitive"}}
            if match_id == "new":
                # They played it, and it records no rank for them - unrated,
                # or a placement. Somebody else's rank is in there and is
                # worth keeping anyway.
                return dict(info, players=[
                    {"subject": "shy", "competitiveTier": 0},
                    {"subject": "bystander", "competitiveTier": 18},
                ])
            return dict(info, players=[{"subject": "shy", "competitiveTier": 21}])

        app = App.__new__(App)
        app.config = validate({}, warn=quiet)
        app.db = db
        app.history_closed = False
        app.view = SimpleNamespace(update=lambda **_kw: None)
        app.client = SimpleNamespace(
            puuid="me", match_history=match_history, match_details=match_details
        )

        rows = [PlayerRow(puuid="shy", hidden=True), PlayerRow(puuid="seen", tier=12)]
        app._rank_hidden(rows)
        ok = check("a hidden stranger gets a rank out of their own matches", rows[0].tier == 21)
        ok &= check("marked as the dated claim it is", bool(rows[0].tier_seen))
        ok &= check(
            "read exactly as far as it had to be",
            asked["details"] == ["new", "older"],
        )
        ok &= check("one history request, not one per match", len(asked["history"]) == 1)
        ok &= check(
            "and it asks for competitive only - no other queue records a rank",
            asked["history"][0][2] == "competitive",
        )
        ok &= check("nobody who already has a rank is asked about", rows[1].tier == 12)
        ok &= check(
            "and what the records said about everyone else is kept too",
            db.last_ranks(["bystander"])["bystander"]["tier"] == 18,
        )

        # Second lobby, same player: every match involved has been read, and a
        # match that has been read is not read again even though it had nothing.
        asked["details"].clear()
        again = [PlayerRow(puuid="shy", hidden=True)]
        app._rank_hidden(again)
        ok &= check("a second lobby costs no requests at all", asked["details"] == [])

        # A player whose competitive history has no rank anywhere in it is
        # Unranked, and the table is now allowed to say so.
        app.history_closed = False
        app.client = SimpleNamespace(
            puuid="me",
            match_history=lambda *_a, **_k: {"History": []},
            match_details=lambda _m: None,
        )
        never = [PlayerRow(puuid="fresh", hidden=True)]
        app._rank_hidden(never)
        ok &= check(
            "no competitive history at all reads as Unranked, not as a gap",
            never[0].unranked and not never[0].tier,
        )

        # Riot closing the route for other people's puuids is worth knowing once.
        shut = SimpleNamespace(status_code=403)
        app.history_closed = False
        app.client = SimpleNamespace(
            puuid="me",
            match_history=lambda *_a, **_k: (_ for _ in ()).throw(
                requests.HTTPError("403", response=shut)
            ),
            match_details=lambda _m: None,
        )
        refused = [PlayerRow(puuid="a", hidden=True), PlayerRow(puuid="b", hidden=True)]
        app._rank_hidden(refused)
        ok &= check("a closed route is noticed", app.history_closed)
        ok &= check(
            "and a refused request never reads as Unranked - it says nothing",
            not refused[0].unranked,
        )

        # And the whole thing is off when the config says so.
        app.config = validate({"rank_hidden_from_history": False}, warn=quiet)
        asked["details"].clear()
        app.client = SimpleNamespace(
            puuid="me", match_history=match_history, match_details=match_details
        )
        off = [PlayerRow(puuid="quiet", hidden=True)]
        app._rank_hidden(off)
        ok &= check("turned off, it asks nothing", not off[0].tier and not asked["details"])
        return ok
    finally:
        db.close()


def check_hidden_form():
    """The other half of the row: the form numbers for a player in Incognito.

    `_rank_hidden` gave a hidden stranger a rank. This gives them the rest of
    what the full report has always printed for the same person - ACS, K/D,
    HS%, the Score - out of the same place: their own finished matches, which
    Incognito does not cover, because Incognito closes a *lookup*.

    The three rules it pins down are the ones that make it affordable. It
    touches hidden players and nobody else; it runs after the visible half of
    the table, so the five people you can see never wait on the five you
    cannot; and it does not spend a request on a route Riot has already shut.
    """
    from valstats.app import App

    db = Encounters(path=":memory:")
    try:
        asked = {"history": [], "details": []}

        def match_history(puuid, count=5, queue="competitive", start=0):
            asked["history"].append(puuid)
            return {"History": [{"MatchID": f"{puuid}-m1"}]}

        def match_details(match_id):
            asked["details"].append(match_id)
            who = match_id.split("-")[0]
            return {
                "matchInfo": {
                    "matchId": match_id,
                    "gameStartMillis": 1757000000000,
                    "queueID": "competitive",
                    "isCompleted": True,
                },
                "players": [
                    {
                        "subject": who,
                        "competitiveTier": 14,
                        "stats": {
                            "score": 5000,
                            "kills": 20,
                            "deaths": 15,
                            "assists": 5,
                            "roundsPlayed": 20,
                        },
                    }
                ],
                "teams": [],
                "roundResults": [],
            }

        app = App.__new__(App)
        app.config = validate({}, warn=quiet)
        app.db = db
        app.history_closed = False
        app.calibration = None
        app.rendered_match = ""
        app.view = SimpleNamespace(status="", update=lambda **_kw: None)
        app.client = SimpleNamespace(
            puuid="me",
            requests_made=0,
            match_history=match_history,
            match_details=match_details,
        )

        rows = [
            PlayerRow(puuid="shy", hidden=True),
            PlayerRow(puuid="loud", tier=12, acs=210.0),
            PlayerRow(puuid="me", hidden=True, is_self=True),
        ]
        app._fill_hidden_performance(rows, own_team="Blue")
        ok = check("the hidden player gets their form numbers", rows[0].acs is not None)
        ok &= check("out of their own matches", asked["history"] == ["shy"])
        ok &= check("and only theirs - nobody visible is fetched again", asked["details"] == ["shy-m1"])
        ok &= check("you are never fetched as a hidden stranger", rows[2].acs is None)
        ok &= check("and the name is still not printed", rows[0].hidden is True)
        ok &= check(
            "the rank rides along free, out of the same downloaded records",
            rows[0].tier == 14 and rows[0].unranked is False,
        )

        # Nothing is asked twice: a second pass over the same table is free,
        # which is what makes it safe to sit behind the visible half.
        asked["history"].clear()
        app._fill_hidden_performance(rows, own_team="Blue")
        ok &= check("a row that is already filled is not fetched again", asked["history"] == [])

        # A route Riot has shut fails identically for every one of them, so
        # the whole pass is skipped rather than paying a request each to learn it.
        app.history_closed = True
        shut_rows = [PlayerRow(puuid="a", hidden=True), PlayerRow(puuid="b", hidden=True)]
        app._fill_hidden_performance(shut_rows, own_team="Blue")
        ok &= check("a closed history route skips the pass outright", asked["history"] == [])

        # And the whole thing is off when the config says so.
        app.history_closed = False
        app.config = validate({"form_hidden_from_history": False}, warn=quiet)
        off = [PlayerRow(puuid="quiet", hidden=True)]
        app._fill_hidden_performance(off, own_team="Blue")
        ok &= check("turned off, it asks nothing", off[0].acs is None and not asked["history"])
        return ok
    finally:
        db.close()


def check_live_odds():
    """The one thing ten rows of facts cannot say: which five are ahead."""
    from valstats.app import App

    db = Encounters(path=":memory:")
    try:
        app = App.__new__(App)
        app.config = validate({}, warn=quiet)
        app.db = db
        app.calibration = None
        shown = {}
        app.view = SimpleNamespace(update=lambda **kw: shown.update(kw))

        def row(team, tier, rr):
            return PlayerRow(puuid=f"{team}{tier}{rr}", team=team, tier=tier, rr=rr, rating=500)

        ours = [row("Blue", 13, 40), row("Blue", 13, 10)]
        theirs = [row("Red", 17, 30), row("Red", 17, 70)]
        app._show_odds(ours + theirs, "Blue")
        lines = shown.get("odds") or []
        ok = check("a two-sided lobby gets its chances", len(lines) >= 2)
        text = " ".join(line.plain for line in lines)
        ok &= check("printed as both sides", "us " in text and "them " in text)
        ok &= check(
            "the stronger side is the one the number favours",
            int(text.split("them ")[1].split("%")[0]) > 50,
        )
        ok &= check("and it says what it was built from", "hidden-MMR pull" in text)
        ok &= check("and that it is a lean", "not a prediction" in text)

        # Agent select: one team on screen, nothing to weigh it against.
        shown.clear()
        app._show_odds(ours, None)
        ok &= check("agent select has no two sides to weigh", "odds" not in shown)

        shown.clear()
        app.config = validate({"show_odds": False}, warn=quiet)
        app._show_odds(ours + theirs, "Blue")
        ok &= check("turned off, it says nothing", "odds" not in shown)
        return ok
    finally:
        db.close()


# ---------------------------------------------------------------------- odds


def _synthetic(scale, per=20, step=50, reach=600):
    """Matches generated by the model itself, in exact proportion.

    Deterministic on purpose: a fit tested against a random sample is a test
    that fails one morning a month for no reason anybody can reproduce.
    """
    rows = []
    for delta in range(-reach, reach + 1, step):
        wins = round(per * odds.logistic(delta / scale))
        rows += [(float(delta), True)] * wins + [(float(delta), False)] * (per - wins)
    return rows


def check_odds():
    ok = True

    # The prior is a statement, not a number pulled out of the air: one
    # division of difference between the two sides is 60/40.
    ok &= check(
        "the prior scale is anchored on one division being 60/40",
        abs(odds.logistic(100 / odds.DEFAULT_SCALE) - 0.6) < 1e-9,
    )

    band = mmr.estimate(13, 50, mmr.read(_run([26] * 5, [-14] * 5)))
    reading = odds.true_rating(13, 50, band)
    ok &= check("a true rating is the rank plus the hidden pull", reading.rr == 1350 + band.gap)
    ok &= check("and carries the band's own width as its error", reading.sigma < odds.RANK_ONLY_SIGMA)
    plain = odds.true_rating(13, 50, None)
    ok &= check("a rank with no band is just the rank", plain.rr == 1350)
    ok &= check(
        "and carries what a rank alone is known to be worth",
        plain.sigma == odds.RANK_ONLY_SIGMA,
    )
    ok &= check("no rank anywhere is no reading", odds.true_rating(0, 0, None) is None)

    # A rank we saw them at last month is an observation about them, which the
    # lobby average is not - so it is used, and it is doubted more.
    old = odds.true_rating(13, 50, band, remembered=True)
    ok &= check("a remembered rank still places them", old.rr == reading.rr)
    ok &= check("but is doubted more than one read today", old.sigma > reading.sigma)
    ok &= check("and is filed as its own kind", old.source == "remembered" and reading.source == "read")

    form = odds.Form(0.4, 200, 0.3, 500)
    moved = odds.true_rating(13, 50, None, rating=600, form=form, mean_rating=500)
    ok &= check("form moves it by the measured slope, not a chosen one", moved.rr == 1350 + 40)
    ok &= check(
        "and not at all when the slope could not be measured",
        odds.true_rating(13, 50, None, rating=600, form=None, mean_rating=500).rr == 1350,
    )

    # A player with no rank is taken as the middle of the lobby rather than
    # dropped, because dropping them changes what the side average is of.
    filled = odds.lobby([odds.Reading(1000.0, 50.0, "read"), odds.Reading(1200.0, 50.0, "read"), None])
    ok &= check("an unranked player is filled in from the lobby", filled[2].rr == 1100)
    ok &= check("and says so by carrying a much wider error", filled[2].sigma == odds.IMPUTED_SIGMA)
    ok &= check("and by saying where it came from", filled[2].source == "lobby")
    ok &= check("a lobby with nobody read fills in nothing", odds.lobby([None, None]) == [None, None])

    readings = [odds.Reading(rr, 50.0, "read") for rr in (1000.0, 1100.0, 1200.0)]
    view = odds.side(readings)
    ok &= check("a side is the mean of its players", view.rr == 1100)
    ok &= check("with three read and none guessed", view.known == 3 and view.imputed == 0)
    mixed = odds.side(readings + [odds.Reading(1100.0, 100.0, "remembered"), odds.Reading(1100.0, 150.0, "lobby")])
    ok &= check(
        "the three kinds are counted apart",
        (mixed.known, mixed.remembered, mixed.imputed) == (3, 1, 1),
    )
    ok &= check("and a placed player is either of the first two", odds.placed(mixed) == 4)

    weak = odds.side(readings)
    strong = odds.side([odds.Reading(1400.0, 50.0, "read") for _ in range(3)])
    chance = odds.chance(weak, strong)
    ok &= check("the stronger side is favoured", chance.theirs > chance.ours)
    ok &= check("and the two add to one", abs(chance.ours + chance.theirs - 1) < 1e-9)
    ok &= check("the answer is a range, not a point", chance.low < chance.theirs < chance.high)
    level = odds.chance(weak, weak)
    ok &= check("two identical sides are even", abs(level.theirs - 0.5) < 1e-9)
    ok &= check("and are called too close to call", odds.too_close(level))
    ok &= check("a clear gap is not", not odds.too_close(chance))

    # Refusals. Two placed players a side, one on a side of one or two. The
    # commonest lobby on a young cache - two of the opposition placed and three
    # guessed - answers, because the guesses carry their own doubt and pull the
    # two sides together rather than apart.
    ok &= check("two placed a side, or one on a side of two", [odds.needed(n) for n in range(6)] == [1, 1, 1, 2, 2, 2])
    five = [odds.Reading(rr, 50.0, "read") for rr in (1000.0, 1100.0)]
    guessed = [odds.Reading(1263.0, odds.IMPUTED_SIGMA, "lobby")] * 3
    ok &= check(
        "two of five placed is enough to answer",
        odds.chance(odds.side(five + guessed), strong) is not None,
    )
    ok &= check(
        "one of five placed is not",
        odds.chance(odds.side(five[:1] + guessed + guessed[:1]), strong) is None,
    )
    ok &= check(
        "and a remembered rank counts towards it",
        odds.chance(
            odds.side([odds.Reading(1000.0, 100.0, "remembered")] * 2 + guessed), strong
        )
        is not None,
    )
    ok &= check("an empty side gets nothing", odds.chance(odds.side([]), strong) is None)
    # Guesses widen, they do not sharpen: the same two readings with three
    # imputed players behind them must answer less confidently, not more.
    sure = odds.chance(odds.side(five), strong)
    unsure = odds.chance(odds.side(five + guessed), strong)
    ok &= check(
        "and a side mostly guessed at answers with a wider range",
        (unsure.high - unsure.low) > (sure.high - sure.low),
    )

    # The scale, fitted. Matches generated by the model at a known scale have
    # to come back as that scale, or the fit is measuring something else.
    for wanted in (150, 250, 400):
        got = odds.fit(_synthetic(wanted))
        ok &= check(
            f"a scale of {wanted} RR is recovered from matches built with it",
            got is not None and abs(got.rr - wanted) < wanted * 0.12,
        )
    ok &= check(
        "and the interval brackets the answer",
        odds.fit(_synthetic(250)).span[0] < 250 < odds.fit(_synthetic(250)).span[1],
    )
    ok &= check("too few matches is no fit", odds.fit(_synthetic(250, per=1)) is None)
    ok &= check("and no matches at all is no fit", odds.fit([]) is None)
    # A season of lobbies where rank predicted nothing must not come back as a
    # scale. It comes back as "no opinion", and the prior stands.
    coin = [(float(delta), index % 2 == 0) for delta in range(-600, 601, 50) for index in range(20)]
    ok &= check("data with no signal in it produces no scale", odds.fit(coin) is None)

    ok &= check(
        "the form slope is measured where the cloud is tight",
        odds.form_fit([(rating, 2.0 * rating + 100) for rating in range(400, 800, 2)]).slope == 2.0,
    )
    ok &= check(
        "and refused where it is not",
        odds.form_fit([(rating, (rating % 7) * 300) for rating in range(400, 800, 2)]) is None,
    )
    ok &= check(
        "too few players is no slope either",
        odds.form_fit([(500, 1000), (600, 1200)]) is None,
    )
    return ok


# ----------------------------------------------------------------- opponents


OPP_NOW = datetime(2026, 3, 1, 22, 0, tzinfo=timezone.utc)


def _opp_line(index, won=1, agent="jett", map_id="ascent", ago=0, queue="competitive"):
    """One cached match line, `ago` minutes before OPP_NOW."""
    return {
        "score": 4800,
        "rounds": 20,
        "kills": 18,
        "deaths": 14,
        "assists": 4,
        "hs": 30,
        "bs": 60,
        "ls": 10,
        "kast": 15,
        "dmg_dealt": 3000,
        "dmg_received": 2600,
        "won": won,
        "agent": agent,
        "team": "Red",
        "party": "",
        "match_id": f"o{index}",
        "map_id": map_id,
        "queue": queue,
        "started_at": (OPP_NOW - timedelta(minutes=ago)).isoformat(),
        "length": 2000,
        "match_score": "13-9",
    }


# ------------------------------------------------------------------ conduct


CONDUCT_DETAILS = {
    "matchInfo": {
        "matchId": "conduct-1",
        "partyRRPenalties": {"party-a": 0.25, "party-b": 0},
    },
    "players": [
        {
            "subject": "afker",
            "partyId": "party-a",
            "accountLevel": 42,
            "sessionPlaytimeMinutes": 95,
            "stats": {"roundsPlayed": 20},
            "behaviorFactors": {
                "afkRounds": 4,
                "stayedInSpawnRounds": 2,
                "friendlyFireOutgoing": 640.6,
                "friendlyFireIncoming": 0,
                "selfDamage": 30,
            },
        },
        {
            "subject": "mate",
            "partyId": "party-a",
            "accountLevel": 300,
            "sessionPlaytimeMinutes": 95,
            "stats": {"roundsPlayed": 20},
            "behaviorFactors": {"afkRounds": 0},
        },
        {
            "subject": "solo",
            "partyId": "party-b",
            "accountLevel": 120,
            "stats": {"roundsPlayed": 20},
            "behaviorFactors": {},
        },
    ],
    "roundResults": [
        {"playerStats": [{"subject": "afker", "wasPenalized": True}]},
        {"playerStats": [{"subject": "afker", "wasPenalized": True}, {"subject": "solo"}]},
    ],
}


BLANK_CONDUCT = dict(conduct.BLANK)


def check_conduct():
    ok = True
    match_id, rows = conduct.extract(CONDUCT_DETAILS)
    ok &= check("conduct carries the match id", match_id == "conduct-1")
    ok &= check("afk rounds read out of behaviorFactors", rows["afker"]["afk_rounds"] == 4)
    ok &= check("friendly fire rounded to whole damage", rows["afker"]["ff_damage"] == 641)
    ok &= check("penalised rounds counted one by one", rows["afker"]["penalised_rounds"] == 2)
    ok &= check("a clean player has none", rows["mate"]["penalised_rounds"] == 0)
    ok &= check("party size counted from the ids", rows["afker"]["party_size"] == 2)
    ok &= check("a solo queue is a party of one", rows["solo"]["party_size"] == 1)
    ok &= check("the party RR penalty is kept in hundredths", rows["afker"]["party_penalty"] == 25)
    ok &= check("and only for the party that got it", rows["solo"]["party_penalty"] == 0)
    ok &= check("session minutes come through", rows["afker"]["session_minutes"] == 95)
    ok &= check("a missing block is zeroes, not a crash", rows["solo"]["afk_rounds"] == 0)

    total = conduct.summarise([rows["afker"], rows["mate"]])
    ok &= check("totals add up", total["afk_rounds"] == 4 and total["rounds"] == 40)
    ok &= check("matches with any afk are counted apart", total["afk_matches"] == 1)
    ok &= check("the account level is the highest seen", total["level"] == 300)
    ok &= check("stacked matches counted", total["stacked_matches"] == 2)
    ok &= check("and both members of a charged party carry it", total["penalised_parties"] == 2)

    flagged = conduct.flags(total)
    ok &= check("a 10% afk rate is worth flagging", any("afk" in note for note in flagged))
    ok &= check("so is the friendly fire", any("own team" in note for note in flagged))
    clean = conduct.summarise([rows["mate"]])
    ok &= check("a clean record raises nothing", conduct.flags(clean) == [])
    ok &= check("and nothing at all is not a record", conduct.summarise([]) is None)

    # A count without a date is an accusation; these are the matches behind it.
    dated = [
        dict(rows["afker"], match_id="m1", started_at="2026-09-14T19:11:00+00:00",
             queue="competitive"),
        dict(rows["mate"], match_id="m2", started_at="2026-09-13T20:00:00+00:00",
             queue="unrated"),
    ]
    found = conduct.incidents(dated, {"m1": 8})
    ok &= check("only the matches where something happened are listed", len(found) == 1)
    ok &= check("dated to the match", found[0].when.startswith("2026-09-14"))
    ok &= check("and named by queue", found[0].queue == "competitive")
    ok &= check("what happened is spelled out", "afk for 4 rounds" in found[0].what)
    ok &= check("penalised rounds too", "penalised in 2 rounds" in found[0].what)
    ok &= check("and what it cost", "-8 RR" in found[0].what)
    ok &= check(
        "a single round reads as one",
        "afk for 1 round," in conduct.incidents(
            [dict(rows["afker"], match_id="m3", afk_rounds=1)]
        )[0].what,
    )
    ok &= check("stray friendly fire is not an incident", conduct.incidents(
        [dict(BLANK_CONDUCT, match_id="m4", ff_damage=40)]
    ) == [])
    ok &= check("nothing in, nothing out", conduct.incidents([]) == [])
    return ok


# ------------------------------------------------------------------ profile


def _profile_line(when, agent_id, kills, deaths, assists, queue="competitive", won=1):
    return {
        "match_id": f"m-{when}-{agent_id}",
        "started_at": when,
        "queue": queue,
        "map_id": ASCENT,
        "agent": agent_id,
        "team": "Red",
        "kills": kills,
        "deaths": deaths,
        "assists": assists,
        "rounds": 20,
        "score": kills * 300,
        "kast": 14,
        "hs": 10,
        "bs": 20,
        "ls": 5,
        "dmg_dealt": 3000,
        "dmg_received": 2800,
        "won": won,
    }


# ----------------------------------------------------------------- evidence


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
    ok &= check_party()
    ok &= check_party_db()
    ok &= check_update()
    ok &= check_party_source()
    ok &= check_own_party()
    ok &= check_party_confirmed()
    ok &= check_share()
    ok &= check_picks()
    ok &= check_by_agent()
    ok &= check_meta()
    ok &= check_client()
    ok &= check_local_api()
    ok &= check_presence_dropout()
    ok &= check_skirmish_state()
    ok &= check_stale_presence()
    ok &= check_stale_version()
    ok &= check_recovery()
    ok &= check_lobby_retry()
    ok &= check_pending_summary()
    ok &= check_rows_from_details()
    ok &= check_heartbeat()
    ok &= check_live_failure()
    ok &= check_identity()
    ok &= check_reveal()
    ok &= check_remembered_ranks()
    ok &= check_loop()
    ok &= check_render(content)
    ok &= check_lookup()
    ok &= check_mmr(content)
    ok &= check_odds()
    ok &= check_hidden_rank()
    ok &= check_hidden_form()
    ok &= check_live_odds()
    ok &= check_conduct()

    print("\n" + ("ALL CHECKS PASSED" if ok else "SOME CHECKS FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
