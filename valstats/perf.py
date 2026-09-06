"""ACS, HS% and K/D over a player's recent competitive matches.

This is the data tracker.gg shows on a profile, computed from the source Riot
serves the client: match-history gives the match ids, match-details gives the
per-round breakdown. One fetched match yields lines for all ten players in it,
so everything lands in the local cache and later lobbies get cheaper.

Two things keep the 0-1000 score honest:
  * a small sample is pulled toward the middle instead of being believed, so
    one lucky match cannot read as 1000;
  * once enough players are cached, the bands are rebuilt from that population
    (see Calibration) rather than from the fixed guesses below.
"""

import bisect
import json
import time

import requests

from .client import ClientUnavailable


# A death counts as traded if your killer dies within this long after you.
TRADE_WINDOW_MS = 3000

# Fixed fallback bands: value -> 0..250, used until a population exists.
BANDS = {
    "acs": (130.0, 300.0),
    "kast": (45.0, 85.0),
    "dd": (-40.0, 60.0),
    "winrate": (20.0, 70.0),
}
METRICS = tuple(BANDS)

# How much evidence a metric needs before it is taken at face value. Below it,
# the value is mixed with the neutral point in proportion to the sample.
SHRINK_ROUNDS = 40  # roughly two matches
SHRINK_MATCHES = 6

# A player needs this many rounds cached before they count as a data point,
# and this many players must qualify before percentile scoring switches on.
MIN_POPULATION_ROUNDS = 40
MIN_POPULATION = 60

BLANK = {
    "score": 0,
    "rounds": 0,
    "kills": 0,
    "deaths": 0,
    "assists": 0,
    "hs": 0,
    "bs": 0,
    "ls": 0,
    "kast": 0,
    "dmg_dealt": 0,
    "dmg_received": 0,
    "won": 0,
}


def extract(details):
    """match_id, {puuid: line} for every player in one match-details payload.

    Deliberately reads only `subject` and the stat blocks. The payload also
    carries gameName/tagLine for every player, including ones hiding behind
    streamer mode - we never touch those fields.
    """
    match_id = (details.get("matchInfo") or {}).get("matchId") or ""
    per_player = {}
    team_of = {}

    for player in details.get("players") or []:
        subject = player.get("subject")
        stats = player.get("stats") or {}
        line = dict(BLANK)
        line.update(
            score=stats.get("score") or 0,
            rounds=stats.get("roundsPlayed") or 0,
            kills=stats.get("kills") or 0,
            deaths=stats.get("deaths") or 0,
            assists=stats.get("assists") or 0,
        )
        per_player[subject] = line
        team_of[subject] = player.get("teamId")

    winners = {team.get("teamId") for team in details.get("teams") or [] if team.get("won")}
    for subject, line in per_player.items():
        line["won"] = 1 if team_of.get(subject) in winners else 0

    for round_result in details.get("roundResults") or []:
        _apply_round(round_result, per_player)

    return match_id, per_player


def outcome(details):
    """Rounds won per team, for the line above the post-match table.

    Same rule as extract(): teams and round counts only, no player fields.
    """
    rounds = {}
    winners = []
    for team in details.get("teams") or []:
        team_id = team.get("teamId")
        if not team_id:
            continue
        rounds[team_id] = team.get("roundsWon") or 0
        if team.get("won"):
            winners.append(team_id)
    return {"rounds": rounds, "winners": winners}


def _apply_round(round_result, per_player):
    """Damage, shot placement and KAST all have to be read round by round."""
    player_stats = round_result.get("playerStats") or []
    present = set()
    kills = []

    for entry in player_stats:
        subject = entry.get("subject")
        present.add(subject)
        kills.extend(entry.get("kills") or [])
        line = per_player.get(subject)
        if not line:
            continue
        for damage in entry.get("damage") or []:
            amount = damage.get("damage") or 0
            line["dmg_dealt"] += amount
            line["hs"] += damage.get("headshots") or 0
            line["bs"] += damage.get("bodyshots") or 0
            line["ls"] += damage.get("legshots") or 0
            receiver = per_player.get(damage.get("receiver"))
            if receiver is not None:
                receiver["dmg_received"] += amount

    # victim -> (killer, when they died)
    deaths = {
        kill.get("victim"): (kill.get("killer"), kill.get("timeSinceRoundStartMillis") or 0)
        for kill in kills
    }
    killers = {kill.get("killer") for kill in kills}
    assisters = {a for kill in kills for a in (kill.get("assistants") or [])}

    for subject in present:
        line = per_player.get(subject)
        if line is None:
            continue
        if subject in killers or subject in assisters or subject not in deaths:
            line["kast"] += 1  # killed, assisted, or survived the round
            continue
        killer, died_at = deaths[subject]
        avenged = deaths.get(killer)
        if avenged and 0 <= avenged[1] - died_at <= TRADE_WINDOW_MS:
            line["kast"] += 1  # traded


def summarise(lines):
    """Weighted totals across matches - not an average of the per-match averages."""
    if not lines:
        return None
    rounds = sum(line["rounds"] for line in lines)
    shots = sum(line["hs"] + line["bs"] + line["ls"] for line in lines)
    deaths = sum(line["deaths"] for line in lines)
    kills = sum(line["kills"] for line in lines)
    delta = sum(line["dmg_dealt"] - line["dmg_received"] for line in lines)
    return {
        "acs": (sum(line["score"] for line in lines) / rounds) if rounds else None,
        "hs": (100.0 * sum(line["hs"] for line in lines) / shots) if shots else None,
        "kd": (kills / deaths) if deaths else float(kills),
        "kast": (100.0 * sum(line["kast"] for line in lines) / rounds) if rounds else None,
        "dd": (delta / rounds) if rounds else None,
        "winrate": 100.0 * sum(line["won"] for line in lines) / len(lines),
        "kills": kills,
        "deaths": deaths,
        "assists": sum(line["assists"] for line in lines),
        "rounds": rounds,
        "matches": len(lines),
    }


def aggregate(lines, calibration=None):
    """summarise() plus the 0-1000 score, which is what the table shows."""
    summary = summarise(lines)
    if summary is None:
        return None
    summary["rating"] = rating(summary, calibration)
    return summary


# ------------------------------------------------------------------- scoring


def _band(value, low, high):
    """Map one metric onto 0-250, flat outside the useful range."""
    if value is None:
        return 0.0
    return max(0.0, min(250.0, 250.0 * (value - low) / (high - low)))


def _shrink(value, neutral, sample, prior):
    """Pull a thin sample toward the middle instead of believing it.

    Five matches is not much and one of them can be an outlier; a player with
    a single 30-kill game should not read as the best in the lobby.
    """
    if value is None or sample is None:
        return value
    if sample <= 0:
        return neutral
    return (sample * value + prior * neutral) / (sample + prior)


def rating(summary, calibration=None):
    """A 0-1000 form score in the spirit of tracker.gg's Tracker Score.

    Same four inputs they use - ACS, KAST, damage delta per round, win rate,
    250 points each. Their number is a percentile against the whole player
    population; with a calibration built from the players this install has met
    it is a percentile too, and without one it falls back to the fixed bands
    below. Either way it will not match the number on their site.

    Shrinking only kicks in when the summary reports how big its sample was,
    so callers can still score a bare set of numbers at face value.
    """
    if not summary:
        return None
    rounds = summary.get("rounds")
    matches = summary.get("matches")

    total = 0.0
    for key in METRICS:
        sample = matches if key == "winrate" else rounds
        prior = SHRINK_MATCHES if key == "winrate" else SHRINK_ROUNDS
        neutral = calibration.neutral(key) if calibration else sum(BANDS[key]) / 2.0
        value = _shrink(summary.get(key), neutral, sample, prior)
        if calibration:
            total += calibration.score(key, value)
        else:
            total += _band(value, *BANDS[key])
    return round(total)


def _quantiles(values, steps=21):
    """`steps` evenly spaced quantiles of a sample, 0th to 100th percentile."""
    ordered = sorted(values)
    if not ordered:
        return []
    if len(ordered) == 1:
        return [float(ordered[0])] * steps
    out = []
    for index in range(steps):
        position = (len(ordered) - 1) * index / (steps - 1)
        low = int(position)
        high = min(low + 1, len(ordered) - 1)
        frac = position - low
        out.append(float(ordered[low]) * (1 - frac) + float(ordered[high]) * frac)
    return out


def _percentile(quantiles, value):
    """Where `value` falls in a quantile table, as 0.0-1.0."""
    if not quantiles or value is None:
        return None
    if value <= quantiles[0]:
        return 0.0
    if value >= quantiles[-1]:
        return 1.0
    index = max(0, bisect.bisect_right(quantiles, value) - 1)
    index = min(index, len(quantiles) - 2)
    low, high = quantiles[index], quantiles[index + 1]
    frac = 0.0 if high == low else (value - low) / (high - low)
    return (index + frac) / (len(quantiles) - 1)


class Calibration:
    """Percentile bands built from every player this install has cached.

    tracker.gg scores against the whole population. We have one too, just a
    smaller and much more relevant one: everybody you have run into, which is
    to say your own rank bracket. It only replaces the fixed bands once enough
    players are in it to mean anything.
    """

    def __init__(self, quantiles, players):
        self.quantiles = quantiles
        self.players = players

    @classmethod
    def from_population(cls, summaries, min_players=MIN_POPULATION):
        """summaries: one dict per player, as produced by Encounters.population()."""
        quantiles = {}
        for key in METRICS:
            values = [s[key] for s in summaries if s.get(key) is not None]
            if len(values) < min_players:
                return None
            quantiles[key] = _quantiles(values)
        return cls(quantiles, len(summaries))

    def neutral(self, key):
        table = self.quantiles.get(key) or []
        return table[len(table) // 2] if table else sum(BANDS[key]) / 2.0

    def score(self, key, value):
        percentile = _percentile(self.quantiles.get(key), value)
        if percentile is None:
            return 0.0
        return 250.0 * percentile

    def describe(self, key):
        """(25th, 50th, 75th percentile) of one metric, for the CLI."""
        table = self.quantiles.get(key) or []
        if not table:
            return None
        last = len(table) - 1
        return tuple(table[round(last * frac)] for frac in (0.25, 0.5, 0.75))

    def to_json(self):
        return json.dumps({"players": self.players, "quantiles": self.quantiles})

    @classmethod
    def from_json(cls, blob):
        try:
            data = json.loads(blob)
            quantiles = {key: [float(v) for v in data["quantiles"][key]] for key in METRICS}
        except (TypeError, ValueError, KeyError):
            return None
        return cls(quantiles, int(data.get("players") or 0))


def load_calibration(db, mode="auto"):
    """The population score for this install, rebuilt when it has gone stale."""
    if mode != "auto" or db is None:
        return None
    stored, players_then = db.calibration()
    population = db.population(MIN_POPULATION_ROUNDS)
    if len(population) < MIN_POPULATION:
        return None
    # Cached bands are fine until the population grows by a tenth.
    if stored and players_then and len(population) < players_then * 1.1:
        cached = Calibration.from_json(stored)
        if cached:
            return cached
    fresh = Calibration.from_population(population)
    if fresh:
        db.store_calibration(fresh.to_json(), len(population))
    return fresh


class PerformanceFetcher:
    """Pulls recent-match numbers, hitting the network only for cache misses."""

    def __init__(self, client, db, count=5, queue="competitive", fallback=True, calibration=None):
        self.client = client
        self.db = db
        self.count = count
        self.queue = queue
        self.fallback = fallback
        self.calibration = calibration
        self.unavailable = False
        self._start_requests = client.requests_made

    @property
    def requests_made(self):
        """Calls that actually went out - cache hits cost nothing."""
        return self.client.requests_made - self._start_requests

    def for_player(self, puuid):
        if self.unavailable or not puuid:
            return None
        match_ids = self._history(puuid)
        if not match_ids:
            return None

        missing = [m for m in match_ids if not self.db.is_parsed(m)]
        for match_id in missing:
            details = self._details(match_id)
            if details is None:
                continue
            parsed_id, per_player = extract(details)
            self.db.store_match_perf(parsed_id or match_id, per_player)

        return aggregate(self.db.perf_rows(puuid, match_ids), self.calibration)

    # ---------------------------------------------------------------- fetches

    def _history(self, puuid):
        ids = self._history_for_queue(puuid, self.queue)
        if not ids and self.fallback and self.queue:
            # Plenty of people only play unrated; a table of dashes helps nobody.
            ids = self._history_for_queue(puuid, None)
        return ids

    def _history_for_queue(self, puuid, queue):
        payload = self._call(lambda: self.client.match_history(puuid, self.count, queue))
        if not payload:
            return []
        return [
            entry.get("MatchID")
            for entry in payload.get("History") or []
            if entry.get("MatchID")
        ]

    def _details(self, match_id):
        return self._call(lambda: self.client.match_details(match_id))

    def _call(self, call):
        try:
            return call()
        except ClientUnavailable:
            try:
                return call()  # tokens were refreshed for us
            except (ClientUnavailable, requests.RequestException):
                return None
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else 0
            if status in (401, 403):
                # Riot has closed this endpoint for other players' puuids.
                self.unavailable = True
            return None
        except requests.RequestException:
            return None


def fetch_details(client, match_id, attempts=4, delay=3.0):
    """One finished match, retried a little: Riot indexes it a beat late."""
    for attempt in range(attempts):
        try:
            details = client.match_details(match_id)
        except ClientUnavailable:
            details = None
        except requests.RequestException:
            details = None
        if details:
            return details
        if attempt < attempts - 1:
            time.sleep(delay)
    return None
