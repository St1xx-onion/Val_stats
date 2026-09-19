"""Turns raw match + MMR payloads into the rows we print."""

from dataclasses import dataclass, field


@dataclass
class PlayerRow:
    puuid: str
    team: str = ""
    agent: str = "-"
    agent_id: str = ""
    name: str = ""
    hidden: bool = False
    # Hidden while the match was live, named afterwards from the match record.
    revealed: bool = False
    # Which of identity.py's sources put this name to this puuid, and - for a
    # source that answers about a moment rather than about today - when that
    # moment was. A dated name is printed as the weaker claim it is: it is who
    # the puuid belonged to then, which is not quite the same as who is here.
    name_source: str = ""
    name_when: str = ""
    # Set when the rank below is the last one we ever saw them at rather than
    # one read for this lobby: the date of that match. See db.last_ranks.
    tier_seen: str = ""
    # Set when the rank came out of the record of the match just finished.
    rank_revealed: bool = False
    # Set when their own match records were read and none of them carries a
    # competitive rank. That is an answer - "this account has never been
    # ranked" - and a different one from the empty cell of somebody nobody was
    # allowed to ask about, so the table prints the two differently.
    unranked: bool = False
    # Set once an MMR lookup has actually been made for this player. False for
    # anyone the live table was not allowed to ask about, which is what tells
    # the post-match reveal who is still missing a rank and a peak.
    mmr_read: bool = False
    level: int = 0
    tier: int = 0
    rr: int = 0
    peak_tier: int = 0
    peak_hidden: bool = False
    wins: int = 0
    games: int = 0
    knife: str = "-"
    seen_before: int = 0
    is_self: bool = False
    # Group letter when this player looks to have queued with someone else in
    # the lobby. Worked out locally from the match cache - see party.py.
    party: str = ""
    # Where the ranked system looks to be walking them, worked out from the RR
    # their own matches paid - see mmr.py. None for anyone with too little
    # ranked history to read, which is most of a fresh cache.
    mmr_band: object = None
    mmr_text: str = ""
    # Recent-form numbers, filled in from match-details when enabled.
    acs: float = None
    hs: float = None
    kd: float = None
    kast: float = None
    dd: float = None
    rating: int = None
    perf_matches: int = 0
    perf_rounds: int = 0
    # What has changed since we last met this player - see progress.py. None
    # for somebody we have never seen, and for yourself.
    progress: object = None
    # Numbers from the single match that just finished, for the summary table.
    final: dict = None
    notes: list = field(default_factory=list)

    @property
    def winrate(self):
        if not self.games:
            return None
        return 100.0 * self.wins / self.games

    def apply_performance(self, summary):
        if not summary:
            return
        self.acs = summary["acs"]
        self.hs = summary["hs"]
        self.kd = summary["kd"]
        self.kast = summary["kast"]
        self.dd = summary["dd"]
        self.rating = summary["rating"]
        self.perf_matches = summary["matches"]
        self.perf_rounds = summary.get("rounds") or 0


def parse_mmr(payload, current_act):
    """Extract current tier/RR, act record and peak tier from an MMR payload."""
    result = {"tier": 0, "rr": 0, "wins": 0, "games": 0, "peak_tier": 0, "peak_hidden": False}
    if not payload:
        return result

    result["peak_hidden"] = bool(payload.get("IsActRankBadgeHidden"))

    seasons = ((payload.get("QueueSkills") or {}).get("competitive") or {}).get(
        "SeasonalInfoBySeasonID"
    ) or {}

    for season_id, info in seasons.items():
        if not isinstance(info, dict):
            continue
        tier = info.get("CompetitiveTier") or 0
        if tier > result["peak_tier"]:
            result["peak_tier"] = tier
        # WinsByTier is the honest peak: it records every tier the player
        # actually won a game in, even if they fell back down afterwards.
        for won_tier, count in (info.get("WinsByTier") or {}).items():
            try:
                won_tier = int(won_tier)
            except (TypeError, ValueError):
                continue
            if count and won_tier > result["peak_tier"]:
                result["peak_tier"] = won_tier
        if current_act and season_id == current_act:
            result["tier"] = tier
            result["rr"] = info.get("RankedRating") or 0
            result["wins"] = info.get("NumberOfWins") or 0
            result["games"] = info.get("NumberOfGames") or 0

    if not result["tier"]:
        latest = payload.get("LatestCompetitiveUpdate") or {}
        result["tier"] = latest.get("TierAfterUpdate") or 0
        result["rr"] = latest.get("RankedRatingAfterUpdate") or 0

    return result


# Queue ids Riot uses; anything unknown is title-cased as it comes.
QUEUE_NAMES = {
    "unrated": "Unrated",
    "competitive": "Competitive",
    "swiftplay": "Swiftplay",
    "spikerush": "Spike Rush",
    "deathmatch": "Deathmatch",
    "ggteam": "Escalation",
    "hurm": "Team Deathmatch",
    "onefa": "Replication",
    "premier": "Premier",
    "newmap": "New Map",
}


def queue_label(match):
    """What to call this match: the queue if it has one, else the game mode.

    Party modes and custom games come through with an empty QueueID, so falling
    back on "unrated" would label a 2v2 as a 5v5 ranked-adjacent queue.
    """
    queue = ((match.get("MatchmakingData") or {}).get("QueueID") or "").strip()
    if queue:
        return QUEUE_NAMES.get(queue.lower(), queue.replace("_", " ").title())
    mode = _mode_name(match.get("ModeID") or match.get("Mode") or "")
    return mode or "custom game"


def _mode_name(mode_id):
    """"/Game/GameModes/Skirmish/SkirmishGameMode..." -> "Skirmish"."""
    parts = [part for part in str(mode_id).split("/") if part]
    for index, part in enumerate(parts):
        if part.lower() == "gamemodes" and index + 1 < len(parts):
            return parts[index + 1]
    return ""


def rows_from_pregame(match, content):
    """Agent-select: only your own team is visible in competitive."""
    rows = []
    for team in match.get("Teams") or []:
        team_id = team.get("TeamID", "")
        for player in team.get("Players") or []:
            rows.append(_row_from_player(player, team_id, content))
    if not rows:
        ally = match.get("AllyTeam") or {}
        for player in ally.get("Players") or []:
            rows.append(_row_from_player(player, ally.get("TeamID", ""), content))
    return rows


def rows_from_coregame(match, content):
    rows = []
    for player in match.get("Players") or []:
        if player.get("IsCoach"):
            continue
        rows.append(_row_from_player(player, player.get("TeamID", ""), content))
    return rows


def _row_from_player(player, team_id, content):
    identity = player.get("PlayerIdentity") or {}
    row = PlayerRow(puuid=player.get("Subject", ""), team=team_id)
    row.agent_id = (player.get("CharacterID") or "").lower()
    row.agent = content.agent(row.agent_id)
    row.hidden = bool(identity.get("Incognito"))
    if not identity.get("HideAccountLevel"):
        row.level = identity.get("AccountLevel") or 0
    # Pregame hands us a tier directly; MMR refines it later.
    row.tier = player.get("CompetitiveTier") or 0
    return row


def attach_knives(rows, loadouts_payload, content):
    """Core-game loadouts come back in the same order as Players."""
    if not loadouts_payload:
        return
    entries = loadouts_payload.get("Loadouts") or []
    by_puuid = {}
    for index, entry in enumerate(entries):
        loadout = entry.get("Loadout") if isinstance(entry.get("Loadout"), dict) else entry
        items = (loadout or {}).get("Items") or {}
        subject = (loadout or {}).get("Subject") or ""
        if subject:
            by_puuid[subject] = items
        elif index < len(rows):
            by_puuid[rows[index].puuid] = items
    for row in rows:
        row.knife = content.skin_name(by_puuid.get(row.puuid))
