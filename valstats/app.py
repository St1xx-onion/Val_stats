"""Main loop: watch the local client state, print the lobby when a match starts."""

import time

import requests

from . import render
from .client import Client, ClientUnavailable
from .config import load as load_config
from .content import Content
from .db import Encounters
from .perf import (
    PerformanceFetcher,
    extract,
    fetch_details,
    load_calibration,
    outcome,
    summarise,
)
from .stats import (
    attach_knives,
    parse_mmr,
    queue_label,
    rows_from_coregame,
    rows_from_pregame,
)

# Sentinel so the very first poll always reports whatever it finds.
UNKNOWN = "<unknown>"

# How many polls to keep re-reading a lobby that would not load. The payload
# usually lags the state change by a beat, but sometimes by rather more.
MAX_ATTEMPTS = 8


class App:
    def __init__(self):
        self.config = load_config()
        self.content = Content().load(want_skins=self.config["show_skins"])
        self.db = Encounters(enabled=self.config["track_encounters"])
        self.client = None
        self.state = UNKNOWN
        self.rendered_match = None
        self.retry = False
        self.attempts = 0
        # The lobby we last showed, kept so the match can be summed up after.
        self.last = None
        self.calibration = None
        self.view = render.MatchView(self.content)

    # ------------------------------------------------------------------- run

    def run(self):
        render.info("Valorant Stats - waiting for the Riot Client...")
        try:
            while True:
                try:
                    self.tick()
                except ClientUnavailable as exc:
                    self.client = None
                    self.state = UNKNOWN
                    self.retry = False
                    self.view.close()
                    render.info(str(exc))
                    time.sleep(5)
                except requests.RequestException as exc:
                    render.warn(f"network hiccup: {exc}")
                    time.sleep(5)
                time.sleep(self.config["poll_interval"])
        except KeyboardInterrupt:
            render.info("bye")
        finally:
            self.view.close()
            self.db.close()

    def tick(self):
        if self.client is None:
            self.client = Client(self.config).connect()
            render.info("connected to the local Riot Client")
            render.banner(self.client, self.content)

        state = self.client.session_state()
        if state == self.state and not self.retry:
            return
        self.state = state

        handled = True
        if state == "PREGAME":
            handled = self.show_pregame()
        elif state == "INGAME":
            handled = self.show_coregame()
        elif state == "MENUS":
            self.enter_menus()
        elif state is None:
            self.rendered_match = None
            self.view.close()
            render.info("Riot Client is up; waiting for VALORANT itself to launch")
        self._after_attempt(handled, state)

    def _after_attempt(self, handled, state):
        """A lobby we could not read is worth another poll, not a lost match."""
        if handled:
            self.retry = False
            self.attempts = 0
            return
        self.attempts += 1
        if self.attempts >= MAX_ATTEMPTS:
            self.retry = False
            self.attempts = 0
            render.warn(f"gave up reading the {str(state).lower()} lobby")
        else:
            self.retry = True

    # --------------------------------------------------------------- screens

    def show_pregame(self):
        match = self._retry(self.client.pregame_match)
        if not match:
            render.info("agent select detected, waiting for the lobby to load...")
            return False
        self._present(rows_from_pregame(match, self.content), "AGENT SELECT - your team")
        return True

    def show_coregame(self):
        match = self._retry(self.client.coregame_match)
        if not match:
            render.info("match started, waiting for the lobby to load...")
            return False
        match_id = match.get("MatchID") or ""
        if match_id and match_id == self.rendered_match:
            return True
        self.rendered_match = match_id

        rows = rows_from_coregame(match, self.content)
        title = f"MATCH LIVE - {queue_label(match)}"
        own_team = self._present(rows, title, match_id=match_id)
        self.db.record(match_id, rows)
        if match_id:
            self.last = (match_id, rows, own_team)
        return True

    def enter_menus(self):
        self.rendered_match = None
        self.view.close()
        if self.config["post_match_summary"] and self.last:
            match_id, rows, own_team = self.last
            self.last = None
            self._post_match(match_id, rows, own_team)
        render.info("in menus - waiting for the next match")

    def _present(self, rows, title, match_id=None):
        """Fill one live table in place: lobby, then ranks, then recent form.

        Agent select and the match itself share the view, so a single block on
        screen grows into the full lobby instead of scrolling a second table.
        """
        for row in rows:
            row.is_self = row.puuid == self.client.puuid
            if row.is_self:
                row.hidden = False
        own_team = self._own_team(rows)

        show_skins = bool(self.config["show_skins"] and match_id)
        self.view.update(
            rows=rows,
            title=title,
            own_team=own_team,
            show_skins=show_skins,
            show_perf=False,
            status="reading the lobby...",
        )

        self._enrich(rows)

        if show_skins:
            try:
                attach_knives(rows, self.client.coregame_loadouts(match_id), self.content)
            except (ClientUnavailable, requests.RequestException):
                pass  # cosmetic only, never worth failing the whole render
        self.view.update(rows=rows, status="")

        if self.config["fetch_performance"]:
            targets = [r for r in rows if not (self.config["respect_streamer_mode"] and r.hidden)]
            if targets:
                self._fill_performance(self._perf_order(targets, own_team))
        return own_team

    @staticmethod
    def _perf_order(targets, own_team):
        """Enemies first: they are why anyone opens this. You are last."""
        return sorted(targets, key=lambda r: (r.is_self, bool(own_team) and r.team == own_team))

    def _fill_performance(self, targets):
        """Ranks are up already; form costs requests, so it lands row by row."""
        count = self.config["performance_matches"]
        self.calibration = load_calibration(self.db, self.config["rating_calibration"])
        fetcher = PerformanceFetcher(
            self.client,
            self.db,
            count=count,
            queue=self.config["performance_queue"],
            fallback=self.config["performance_queue_fallback"],
            calibration=self.calibration,
        )
        for done, row in enumerate(targets, start=1):
            row.apply_performance(fetcher.for_player(row.puuid))
            if fetcher.unavailable:
                break
            self.view.update(
                show_perf=True,
                status=f"last {count} matches - {done}/{len(targets)} players",
            )

        if fetcher.unavailable:
            self.view.update(status="")
            render.warn("match history is not readable for other players - skipping ACS/HS%")
            return
        if not any(r.acs is not None for r in targets):
            self.view.update(status="")
            render.warn("no recent matches found for anyone in this lobby")
            return

        scale = (
            f"scored against {self.calibration.players} players you have met"
            if self.calibration
            else "scored against the fixed reference bands"
        )
        self.view.update(
            show_perf=True,
            status=(
                f"form over the last {count} matches - {fetcher.requests_made} requests, "
                f"the rest came from the local cache; {scale}"
            ),
        )

    # ------------------------------------------------------------ post-match

    def _post_match(self, match_id, rows, own_team):
        """The scoreboard for the match you just played.

        One request for a match of your own, which also drops the lines of all
        ten players into the cache for free.
        """
        render.info("match over - reading the result...")
        details = fetch_details(self.client, match_id)
        if not details:
            render.warn("the finished match was not published yet - no summary this time")
            return

        parsed_id, per_player = extract(details)
        self.db.store_match_perf(parsed_id or match_id, per_player)
        for row in rows:
            line = per_player.get(row.puuid)
            row.final = summarise([line]) if line else None

        result = outcome(details)
        render.console.print()
        render.console.print(
            render.build_summary_table(
                rows,
                self._result_title(result, own_team),
                own_team=own_team,
                caption=self._rr_caption(match_id),
            )
        )

    @staticmethod
    def _result_title(result, own_team):
        rounds = result.get("rounds") or {}
        if own_team and own_team in rounds:
            mine = rounds[own_team]
            theirs = max((v for k, v in rounds.items() if k != own_team), default=0)
            verdict = "WON" if own_team in (result.get("winners") or []) else "LOST"
            if mine == theirs:
                verdict = "DRAW"
            return f"MATCH OVER - {verdict} {mine}:{theirs}"
        score = " : ".join(str(v) for v in rounds.values())
        return f"MATCH OVER - {score}" if score else "MATCH OVER"

    def _rr_caption(self, match_id):
        """Your own RR movement, if Riot has already booked it for this match."""
        try:
            payload = self.client.mmr(self.client.puuid, fresh=True)
        except (ClientUnavailable, requests.RequestException):
            return None
        latest = (payload or {}).get("LatestCompetitiveUpdate") or {}
        if not latest or latest.get("MatchID") not in (match_id, None):
            return None
        earned = latest.get("RankedRatingEarned")
        if earned is None:
            return None
        tier = self.content.tier(latest.get("TierAfterUpdate") or 0)["name"]
        after = latest.get("RankedRatingAfterUpdate") or 0
        return f"{earned:+d} RR -> {tier} {after} RR"

    # ---------------------------------------------------------------- detail

    def _enrich(self, rows):
        respect = self.config["respect_streamer_mode"]
        visible = [r for r in rows if not (respect and r.hidden)]

        names = {}
        if visible:
            names = self._retry(lambda: self.client.names([r.puuid for r in visible])) or {}
        for row in visible:
            row.name = names.get(row.puuid, "")
            if not row.name:
                # Blank from the name service means the player hid themselves.
                row.hidden = True

        wanted = [r for r in rows if not (respect and r.hidden)]
        for done, row in enumerate(wanted, start=1):
            payload = self._retry(lambda p=row.puuid: self.client.mmr(p))
            parsed = parse_mmr(payload, self.content.current_act)
            row.tier = parsed["tier"] or row.tier
            row.rr = parsed["rr"]
            row.wins = parsed["wins"]
            row.games = parsed["games"]
            row.peak_tier = parsed["peak_tier"]
            # Riot lets players hide their act rank badge - honour that.
            row.peak_hidden = parsed["peak_hidden"] or not self.config["show_peak_rank"]
            self.view.update(status=f"ranks - {done}/{len(wanted)} players")

        counts = self.db.counts([r.puuid for r in rows if not r.is_self])
        for row in rows:
            row.seen_before = counts.get(row.puuid, 0)

    @staticmethod
    def _own_team(rows):
        for row in rows:
            if row.is_self:
                return row.team
        return None

    @staticmethod
    def _retry(call, attempts=2, delay=1.0):
        """Match payloads lag the state change by a beat; tokens can expire."""
        for attempt in range(attempts):
            try:
                result = call()
            except ClientUnavailable:
                result = None  # tokens were refreshed for us; try again
            except requests.RequestException:
                result = None  # one flaky call must not sink the whole lobby
            if result:
                return result
            if attempt < attempts - 1:
                time.sleep(delay)
        return None


def main():
    App().run()
