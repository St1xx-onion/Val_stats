"""Main loop: watch the local client state, print the lobby when a match starts."""

import faulthandler
import threading
import time
import traceback
from datetime import datetime, timezone

import requests

from . import identity as identity_module
from . import mmr as mmr_module
from . import odds as odds_module
from . import party as party_module
from . import pool as pool_module
from . import progress as progress_module
from . import share
from . import update
from . import picks as picks_module
from . import render
from .client import Client, ClientUnavailable
from .config import ROOT
from .config import load as load_config
from .content import Content
from .db import Encounters
from .perf import (
    PerformanceFetcher,
    extract,
    fetch_details,
    load_calibration,
    meta,
    outcome,
    summarise,
    tiers as reveal_tiers,
)
from .stats import (
    PlayerRow,
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

# And when even that was not enough, how long to leave it before looking again.
# A lobby can be slower than any number of quick attempts - a long loading
# screen, a server having a moment - and a match is forty minutes long, so
# giving up on one for good is the expensive mistake here, not another request.
# Each further failure waits a step longer, up to five minutes.
RETRY_AFTER = 30.0
MAX_RETRY_AFTER = 300.0

# A poll that has not come back in this long is not working, it is stuck.
STALL_AFTER = 120.0

# The match you just played is not in Riot's records the instant it ends. The
# first few tries happen on the spot, while "match over - reading the result"
# is still on screen; after that the wait moves into the poll loop, one request
# every SUMMARY_RETRY_EVERY seconds, for as long as summary_wait_seconds says.
SUMMARY_TRIES = 3
SUMMARY_TRY_EVERY = 2.0
SUMMARY_RETRY_EVERY = 10.0

# And when summary_wait_seconds runs out, the match is still not given up on.
# It goes quiet into the cache's pending queue and is asked for at this slower
# cadence - between matches only, never over a live lobby - and again every
# time the program starts. Riot has published matches a good deal later than
# any reasonable person waits at the screen, and a scoreboard that arrives late
# is worth incomparably more than one that was thrown away on a timer.
SUMMARY_SLOW_EVERY = 300.0

# Ticks that went wrong in a row before the whole connection is rebuilt from
# the lockfile, and how long to hold off in the meantime.
RESET_AFTER = 5
ERROR_SLEEP = 5.0
MAX_ERROR_SLEEP = 30.0

# Starting up needs the content servers and the database; both can be busy.
STARTUP_ATTEMPTS = 5
STARTUP_RETRY = 10.0

# Static content is cached on disk for a day, but this process is meant to sit
# running for days at a time - and when an act rolls over, an old act uuid
# reads every player in the lobby as unranked.
CONTENT_REFRESH = 6 * 60 * 60

# Full tracebacks go here: the console will have scrolled past by the time
# anyone reads it, and "it broke while I was away" needs an answer.
LOG_PATH = ROOT / "errors.log"
MAX_LOG_BYTES = 512 * 1024


def log_stall():
    """Write down where every thread is. A hang leaves no traceback of its own."""
    try:
        with LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(
                f"\n=== {datetime.now().isoformat(timespec='seconds')} stalled ===\n"
            )
            handle.flush()
            faulthandler.dump_traceback(file=handle)
        return LOG_PATH.name
    except (OSError, RuntimeError, ValueError):
        return None


def log_crash(exc):
    """Append one traceback to the log file. Best effort - never fatal."""
    try:
        if LOG_PATH.is_file() and LOG_PATH.stat().st_size > MAX_LOG_BYTES:
            LOG_PATH.unlink()
        with LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(f"\n=== {datetime.now().isoformat(timespec='seconds')} ===\n")
            handle.write("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
        return LOG_PATH.name
    except OSError:
        return None


class App:
    def __init__(self):
        self.config = load_config()
        self.content = Content().load(want_skins=self.config["show_skins"])
        self.db = Encounters(enabled=self.config["track_encounters"])
        self.client = None
        # Built once the client is up, because most of the sources need it.
        self.identity = None
        # Where this lobby's hidden players were named from, until it is said.
        self.named_live = ""
        # And the same for the scoreboard printed after the match.
        self.named_from = ""
        self.state = UNKNOWN
        self.rendered_match = None
        self.retry = False
        self.attempts = 0
        # The lobby we last showed, kept so the match can be summed up after.
        self.last = None
        # A match that ended before Riot published it: the lobby it needs, when
        # to ask again, and when the fast asking gives way to the quiet queue
        # that outlives this process. See _defer_summary.
        self.pending_summary = None
        self.summary_at = 0.0
        self.summary_until = 0.0
        self.summary_quiet = False
        # Next sweep of matches queued by an earlier lobby or an earlier run.
        # Zero so the first quiet moment after startup does one.
        self.resume_at = 0.0
        self.calibration = None
        self.calibrated = False
        # mmr.py's convergence constant, measured against the cache. Same
        # read-once shape as the calibration above and for the same reason.
        self.convergence = None
        self.convergence_read = False
        # Agent select, kept so locking allies can refresh the pick advice.
        self.pregame_id = ""
        self.pregame_map = ""
        self.pregame_rows = None
        self.pregame_at = 0.0
        # Last time we re-read the chat room for hidden players, mid-match.
        self.harvest_at = 0.0
        self.own_lines = None
        self.view = render.MatchView(self.content, show_peak=self.config["show_peak_rank"])
        self.content_at = time.monotonic()
        self.failures = 0
        # A lobby we could not read, and when it is worth another look.
        self.retry_at = 0.0
        self.gave_up = 0
        # Liveness: when the poll running right now started, and the last time
        # we said out loud that we are still here.
        self.tick_started = 0.0
        self.stalled = False
        self.beat_at = time.monotonic()
        self.presence_stale = 0
        self.probe_failed = 0
        # Set once Riot answers 401/403 for another player's match history:
        # the route is shut for this session and retrying it is ten wasted
        # requests a lobby. See _history_call.
        self.history_closed = False

    # ------------------------------------------------------------------- run

    def run(self):
        """Watch, forever. Nothing short of Ctrl-C is allowed to end this.

        The whole value of the thing is that it is already running when a match
        starts, so every failure has to be survivable: a dropped connection, a
        payload in a shape we did not expect, a client that was restarted under
        us. What we cannot handle in place, we recover from by rebuilding the
        connection - which is what restarting the program used to do by hand.
        """
        render.info("Valorant Stats - waiting for the Riot Client...")
        threading.Thread(target=self._watchdog, daemon=True).start()
        try:
            while True:
                self.tick_started = time.monotonic()
                try:
                    self.watch_once()
                except Exception as exc:  # noqa: BLE001 - even the recovery can fail
                    log_crash(exc)
                    time.sleep(ERROR_SLEEP)
                finally:
                    self.tick_started = 0.0
                    if self.stalled:
                        self.stalled = False
                        render.info("...and the poll came back", repeat=True)
                time.sleep(self.config["poll_interval"])
        except KeyboardInterrupt:
            render.info("bye")
        finally:
            self.view.close()
            self.db.close()

    def _watchdog(self):
        """Notice a poll that stopped coming back, and write down where it is.

        Every request in here carries a timeout, which is the kind of promise
        that turns out to have an exception in it: a socket that never returns,
        a console write parked behind a text selection in the terminal window.
        This cannot unstick any of that. What it can do is turn a program that
        hangs looking exactly like a quiet evening into one that says so, and
        leaves a stack in the log for whoever comes to ask why.
        """
        while True:
            time.sleep(STALL_AFTER / 4)
            started = self.tick_started
            if self.stalled or not started:
                continue
            if time.monotonic() - started < STALL_AFTER:
                continue
            self.stalled = True
            # The log first: the console may well be where this is stuck.
            written = log_stall()
            where = f" - the stacks are in {written}" if written else ""
            render.warn(f"a poll has been stuck for over {STALL_AFTER:.0f}s{where}")

    def watch_once(self):
        """One poll and whatever recovery it turns out to need."""
        try:
            self.tick()
            self.failures = 0
        except ClientUnavailable as exc:
            self.reconnect(str(exc))
        except requests.RequestException as exc:
            self.stumble(f"network hiccup: {exc}")
        except Exception as exc:  # noqa: BLE001 - see run()
            self.stumble(f"unexpected error: {exc.__class__.__name__}: {exc}", exc)

    def reconnect(self, why):
        """Start over from the lockfile: whatever we were talking to is gone."""
        self.client = None
        self.identity = None
        self.failures = 0
        self.own_lines = None
        self.forget_screen()
        render.info(why)
        time.sleep(ERROR_SLEEP)

    def stumble(self, message, exc=None):
        """One tick went wrong. Say so, back off, and keep watching.

        An unexpected exception also drops the current screen: the tick that
        raised it was halfway through drawing, and forgetting what is on screen
        makes the next poll redraw the lobby from scratch instead of leaving a
        half-filled table sitting there until the match ends.
        """
        self.failures += 1
        render.warn(message)
        if exc is not None:
            written = log_crash(exc)
            if written:
                render.info(f"the full traceback is in {written}", repeat=True)
            self.forget_screen()
        if self.failures >= RESET_AFTER:
            self.reconnect("too many errors in a row - reconnecting to the client")
            return
        time.sleep(min(MAX_ERROR_SLEEP, ERROR_SLEEP * self.failures))

    def forget_screen(self):
        """Drop what we think is on screen so the next poll draws it again."""
        self.state = UNKNOWN
        self.rendered_match = None
        self.retry = False
        self.attempts = 0
        self.retry_at = 0.0
        self.gave_up = 0
        self._forget_pregame()
        self.view.close()

    def tick(self):
        if self.client is None:
            self.client = Client(self.config).connect()
            # `who` and `match` run with the game closed, and need to know
            # which of the ten puuids in a cached match was yours.
            self.db.remember_own(self.client.puuid)
            self.identity = identity_module.Resolver.build(
                self.config, client=self.client, db=self.db, content=self.content
            )
            render.info("connected to the local Riot Client")
            render.banner(self.client, self.content)
        else:
            # Tokens expire after about an hour of sitting in menus.
            self.client.keep_fresh()

        state = self.client.session_state()
        self._note_presence(state)
        self._poll_summary(state)
        self._resume_summaries(state)
        due = bool(self.retry_at) and time.monotonic() >= self.retry_at
        if state == self.state and not self.retry and not due:
            if state == "PREGAME":
                self.refresh_pregame()
            elif state == "INGAME":
                self.reharvest_hidden()
            self._heartbeat(state)
            return
        self.state = state
        self.retry_at = 0.0
        self.beat_at = time.monotonic()

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
            self.gave_up = 0
            return
        self.attempts += 1
        if self.attempts < MAX_ATTEMPTS:
            self.retry = True
            return
        # Out of quick attempts - but not out of match. Whatever this lobby is
        # waiting on can easily outlast eight polls, and the state we are in
        # lasts for the next forty minutes, so this backs off instead of
        # writing the whole match off the way it used to.
        self.retry = False
        self.attempts = 0
        self.gave_up += 1
        wait = min(MAX_RETRY_AFTER, RETRY_AFTER * self.gave_up)
        self.retry_at = time.monotonic() + wait
        render.warn(
            f"could not read the {str(state).lower()} lobby - "
            f"trying again in {wait:.0f}s"
        )

    def _note_presence(self, state):
        """Say so when either source of the state is caught failing.

        Both failures used to be completely silent, and silent is what made
        them expensive: the table never appears, the last line on screen still
        says we are in menus, and nothing about that looks like a fault.
        """
        stale = getattr(self.client, "presence_stale", 0)
        if stale > self.presence_stale:
            self.presence_stale = stale
            render.warn(
                "the chat presence says MENUS but the game servers say "
                f"{str(state).lower()} - going by the servers"
            )
        failed = getattr(self.client, "probe_failed", 0)
        if failed > self.probe_failed:
            self.probe_failed = failed
            render.warn(
                "the game servers would not say which screen we are on - "
                f"holding {str(state).lower()} and retrying"
            )

    def _heartbeat(self, state):
        """Now and then, while nothing happens, say that nothing is happening.

        A watcher sitting quietly and a watcher that wedged solid leave the
        same thing on screen: the last line it printed, an hour ago. A clock
        that keeps moving is the difference between the two.
        """
        interval = self.config["heartbeat_minutes"] * 60
        if not interval or state not in ("MENUS", None):
            return
        now = time.monotonic()
        if now - self.beat_at < interval:
            return
        self.beat_at = now
        where = "in menus" if state == "MENUS" else "waiting for VALORANT"
        note = " (the chat presence has been unreliable)" if self.presence_stale else ""
        if self.probe_failed:
            note = " (the game servers have been refusing to say)"
        render.info(
            f"still watching - {where} at {datetime.now().strftime('%H:%M:%S')}{note}",
            repeat=True,
        )

    # --------------------------------------------------------------- screens

    def show_pregame(self):
        match = self._retry(self.client.pregame_match)
        if not match:
            render.info("agent select detected, waiting for the lobby to load...")
            return False
        self.pregame_id = match.get("ID") or match.get("MatchID") or ""
        self.pregame_map = (match.get("MapID") or "").lower()
        self.pregame_at = time.monotonic()
        rows = rows_from_pregame(match, self.content)
        self.pregame_rows = rows
        self._present(
            rows, self._title("AGENT SELECT", self.pregame_map, "your team"), advise=True
        )
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
        self._forget_pregame()

        rows = rows_from_coregame(match, self.content)
        title = self._title("MATCH LIVE", (match.get("MapID") or "").lower(), queue_label(match))
        own_team = self._present(rows, title, match_id=match_id)
        self.db.record(match_id, rows)
        # After record(), because remember_found only renames a player who is
        # already logged as an encounter - and record() is what logs them.
        self._harvest_hidden(rows)
        if match_id:
            self.last = (match_id, rows, own_team)
        return True

    def _title(self, prefix, map_id, tail):
        name = self.content.map_name(map_id)
        return f"{prefix} - {tail}" if not name else f"{prefix} - {name} - {tail}"

    def _forget_pregame(self):
        self.pregame_id = ""
        self.pregame_map = ""
        self.pregame_rows = None

    def refresh_pregame(self):
        """Agent select moves under us as allies lock in, so the advice follows.

        Only the agents on the roster can still change, so this patches those in
        place instead of rebuilding rows that already carry ranks. One request,
        and only once the interval has passed - see pregame_refresh in the config.
        """
        interval = self.config["pregame_refresh"]
        if not interval or not self.pregame_id or not self.pregame_rows:
            return
        now = time.monotonic()
        if now - self.pregame_at < interval:
            return
        self.pregame_at = now
        try:
            match = self.client.pregame_match(self.pregame_id)
        except (ClientUnavailable, requests.RequestException):
            return  # the lobby is about to end anyway; nothing worth reporting
        if not match:
            return

        fresh = {row.puuid: row for row in rows_from_pregame(match, self.content)}
        changed = False
        for row in self.pregame_rows:
            new = fresh.get(row.puuid)
            if new and new.agent_id != row.agent_id:
                row.agent_id, row.agent = new.agent_id, new.agent
                changed = True
        if not changed:
            return  # a redraw that shows the same thing is just flicker
        found, heading = self._picks(self.pregame_rows, self.pregame_map)
        self.view.update(rows=self.pregame_rows, picks=found, picks_heading=heading)

    def enter_menus(self):
        self.rendered_match = None
        self._forget_pregame()
        self.view.close()
        if self.config["post_match_summary"] and self.last:
            match_id, rows, own_team = self.last
            self.last = None
            self._post_match(match_id, rows, own_team)
        self._refresh_content()
        self._share_due()
        self._pool_due()
        render.info("in menus - waiting for the next match")

    def _share_due(self):
        """Hand the shared pool whatever has piled up, between matches.

        Here and nowhere else: sharing must never compete with a live lobby for
        the connection, and by this point the match that just ended is parsed
        and in the cache. Failure is silent by design - a pool that is down is
        not a reason to put a warning over somebody's menus - and costs
        nothing, because the queue lives in the database and the next pass
        picks it up.
        """
        if not self.config.get("share_stats"):
            return
        try:
            sent = share.push(self.config, self.db)
        except Exception as exc:  # noqa: BLE001 - never worth a crash
            log_crash(exc)
            return
        if sent:
            render.info(f"shared {sent} matches with the pool")

    def _pool_due(self):
        """Fetch the shared pool between matches, if it has gone stale.

        Same rules as _share_due: never during a lobby, never a reason to make
        a noise when it fails, and the copy already in the database keeps
        working either way.
        """
        if not self.config.get("use_pool"):
            return
        try:
            got = pool_module.sync(self.config, self.db)
        except Exception as exc:  # noqa: BLE001 - never worth a crash
            log_crash(exc)
            return
        if got:
            render.info(f"pool refreshed: {got} matches from other installs")

    def _refresh_content(self):
        """Re-read the static content between matches, once in a while.

        Acts roll over, and this process is meant to stay up for days: with a
        stale act uuid every player in the lobby reads as unranked. The files
        are cached on disk for a day, so this normally touches no network at
        all - and if it does and fails, the copy we already have still works.
        """
        now = time.monotonic()
        if now - self.content_at < CONTENT_REFRESH:
            return
        self.content_at = now
        try:
            self.content.load(want_skins=self.config["show_skins"])
        except (requests.RequestException, OSError, ValueError, KeyError, IndexError) as exc:
            render.warn(f"could not refresh the game content: {exc}")

    def _picks(self, rows, map_id):
        """The short list of agents worth taking, and what it is based on.

        Costs nothing but a local query: roles come from the content cache and
        your own record from the encounters database. The one call that can go
        out is the entitlements check, and only when owned_agents_only is set.
        """
        if not self.config["recommend_picks"]:
            return None, ""
        if self.own_lines is None:
            self.own_lines = self.db.all_perf_rows(self.client.puuid)
        if not self.calibrated:
            self.calibration = load_calibration(self.db, self.config["rating_calibration"])
            self.calibrated = True

        pool = None
        if self.config["recommend_owned_agents_only"]:
            pool = self.client.owned_agents()
        taken = picks_module.taken_agents(rows)
        found = picks_module.recommend(
            self.content,
            taken,
            self.own_lines,
            map_id=map_id,
            pool=pool,
            calibration=self.calibration,
        )
        if not found:
            return None, ""

        cached = sum(1 for line in self.own_lines if line.get("agent"))
        locked = f"{len(taken)} ally agent{'s' if len(taken) != 1 else ''} known"
        basis = (
            f"your {cached} cached matches"
            if cached
            else "team composition only - none of your matches are cached yet"
        )
        return found, f"Suggested picks  ({locked}, {basis})"

    def _present(self, rows, title, match_id=None, advise=False):
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
            picks=None,
            picks_heading="",
            odds=None,
        )

        if advise:
            # Nothing here waits on the network, so the advice is on screen
            # before the first rank arrives - which is when it is still useful.
            found, heading = self._picks(rows, self.pregame_map)
            self.view.update(picks=found, picks_heading=heading)

        self._enrich(rows)

        if show_skins:
            try:
                attach_knives(rows, self.client.coregame_loadouts(match_id), self.content)
            except (ClientUnavailable, requests.RequestException):
                pass  # cosmetic only, never worth failing the whole render
        self.view.update(rows=rows, status="")

        # Everything from here fills the table in where it stands. The whole
        # table is on screen first and the slow columns arrive into it, rather
        # than the screen waiting on the slowest thing anybody asked for.
        self._rank_hidden(rows)

        if self.config["fetch_performance"]:
            # The visible half first, always: they are what the table is open
            # for, and nobody should wait on a stranger who is hiding.
            targets = [r for r in rows if not r.hidden]
            if targets:
                self._fill_performance(self._perf_order(targets, own_team))
            # And then the players in Incognito, out of their own matches -
            # the same pass the full report makes, made here. See below.
            self._fill_hidden_performance(rows, own_team)

        self._estimate_mmr(rows)
        # Last of the lot, and it has to be: it is the only thing here built
        # out of the others rather than out of a request, so every column it
        # reads is already on the table by the time it runs.
        self._show_odds(rows, own_team)
        self._mark_progress(rows, match_id)
        self._mark_parties(rows, own_team, match_id)
        self._note_named()
        self._note_remembered(rows)
        return own_team

    def _show_odds(self, rows, own_team):
        """The two sides' chances, under the table, out of numbers already up.

        The same model as the full report - rank, plus the hidden-MMR pull,
        plus form converted at a slope measured on this machine's own cache -
        run over the rows the table is already holding. It costs no requests
        of its own: everything in it was paid for by the columns above.

        What makes it worth having anyway is that it is the one thing the
        table cannot say. Ten rows of ranks and ACS are ten facts about ten
        people, and the question anybody actually has in front of a lobby is
        about the two fives. The answer is shallower here than in the full
        report and the caption says so: the form behind it is
        performance_matches deep, five by default, against the report's thirty.

        Silent in agent select, where there is one team on screen and nothing
        to weigh it against.
        """
        if not self.config["show_odds"] or not own_team:
            return
        ours = [row for row in rows if row.team == own_team]
        theirs = [row for row in rows if row.team and row.team != own_team]
        if not ours or not theirs:
            return
        lineup = ours + theirs
        form = odds_module.load_form(self.db, getattr(self, "calibration", None))
        # A rank we saw them at before, for anyone with none in hand. Same
        # rule as the full report: wider error, and counted separately, but an
        # observation about them rather than about the people standing nearby.
        remembered = self.db.last_ranks([row.puuid for row in lineup if not row.tier])
        readings = [
            odds_module.true_rating(
                *self._odds_rank(row, remembered),
                row.mmr_band,
                row.rating,
                form,
                form.middle if form else None,
                remembered=not row.tier or bool(row.tier_seen),
            )
            for row in lineup
        ]
        everyone = odds_module.lobby(readings)
        us = odds_module.side(everyone[: len(ours)])
        them = odds_module.side(everyone[len(ours) :])
        chance = odds_module.chance(us, them, odds_module.load_scale(self.db))
        if chance is None:
            return
        self.view.update(odds=self._odds_lines(chance, us, them))

    @staticmethod
    def _odds_rank(row, remembered):
        """(tier, rr) for the model: the rank on the row, else one on record."""
        if row.tier:
            return row.tier, row.rr
        seen = (remembered or {}).get(row.puuid) or {}
        return seen.get("tier") or 0, seen.get("rr") or 0

    def _odds_lines(self, chance, us, them):
        """The number, the width, and what it is worth - three lines at most."""
        line = render.Text("  ")
        line.append(f"us {chance.ours * 100:.0f}%", style="bold green")
        line.append("   ")
        line.append(f"them {chance.theirs * 100:.0f}%", style="bold red")
        low, high = sorted((chance.low * 100, chance.high * 100))
        line.append(f"   {100 - high:.0f}-{100 - low:.0f}% for us across the error", style="dim")
        if odds_module.too_close(chance):
            line.append("   too close to call", style="dim")
        depth = self.config["performance_matches"]
        built = (
            f"rank + hidden-MMR pull + form over {depth} matches"
            if self.config["fetch_performance"]
            else "rank + hidden-MMR pull"
        )
        lines = [line, render.Text(f"  {built} - a lean, not a prediction", style="dim")]
        # Second caveat line only when there is a caveat. Two lines under a
        # live table is already most of what it can spare.
        caveats = []
        if not chance.measured:
            caveats.append("scale is the stated 60/40 prior, not measured yet")
        guessed = us.imputed + them.imputed
        if guessed:
            caveats.append(f"{guessed} of {us.count + them.count} had no rank, taken as the lobby average")
        if caveats:
            lines.append(render.Text("  " + "; ".join(caveats), style="dim"))
        return lines

    def _estimate_mmr(self, rows):
        """The rank the system is walking each player towards. See mmr.py.

        One request per player, and only for players who have a rank to
        measure the answer against. It runs after the form numbers because it
        is the least urgent column on the table and the one most likely to
        come back empty: a shard that has closed the route or a player with no
        ranked history both end here with no band, and the column drops out of
        the table on its own when nobody in the lobby has one.

        Immortal and above are asked about like everybody else, and get the
        pull without a rank on it - "+38 RR" rather than a division. The
        ladder arithmetic is what stops above Immortal, not the measurement,
        and the lobbies where this column is most interesting are the ones
        that used to have it empty.

        The rows are cached as they arrive, so the deep report never asks for
        a player this has already read - and so does the scoreboard after the
        match, which picks up the same rows and only has to ask about whoever
        was hiding while this ran. See _read_bands.
        """
        if not self.config["estimate_mmr"]:
            return
        # Nobody the client is hiding. The band is read out of that player's own
        # RR history, which is precisely the sort of lookup _enrich declines to
        # make about somebody in Incognito: the rank on their row was
        # remembered from an earlier meeting and cost no request, where this
        # would be a request about them, now, while they are hiding.
        wanted = [
            row
            for row in rows
            if not (self.config["respect_streamer_mode"] and row.hidden)
        ]
        if self._read_bands(wanted):
            self.view.update(rows=rows)

    def _read_bands(self, rows):
        """Fill in the hidden-rating band on every row that still wants one.

        Shared by the live table and the scoreboard printed after the match.
        Rows that already carry a band are skipped, which is what makes the
        second pass free in the ordinary case: the ten bands were read while
        the lobby was on screen, and the only people left are the ones nobody
        was allowed to ask about at the time.
        """
        count = self.config["mmr_matches"]
        found = 0
        for row in rows:
            if not row.puuid or not row.tier or row.mmr_band is not None:
                continue
            updates = self._rr_history(row.puuid, count)
            if not updates:
                continue
            reading = mmr_module.read(updates)
            row.mmr_band = mmr_module.estimate(row.tier, row.rr, reading, self._convergence())
            if row.mmr_band:
                row.mmr_text = mmr_module.describe(row.mmr_band, self.content)
                found += 1
        return found

    def _rr_history(self, puuid, count):
        """One player's recent RR movements, from Riot or from the cache.

        Riot first, because the history is one match longer every time they
        play and the band is meant to say where the system has them *now*. The
        cache is the fallback rather than the first stop - but it is a
        fallback, which it was not before: one player's missing band is not
        worth stopping the lobby for, and a route that has closed used to empty
        the whole column even though the same matches were sitting on disk.
        """
        try:
            updates = self.client.competitive_updates(puuid, count)
        except (ClientUnavailable, requests.RequestException):
            # A route that has closed will fail for all ten - and each of those
            # failures is one request, not a retry.
            updates = None
        if updates:
            self.db.store_rr_updates(puuid, updates)
            return updates
        return self.db.rr_updates_for(puuid, count)

    def _convergence(self):
        """mmr.py's constant, measured against the cache once per session.

        Read once and kept: it is arithmetic over every RR history on the
        machine, it moves by a hair when one more match lands in there, and
        the lobby is the last place to spend a tenth of a second on something
        that will give the same answer it gave an hour ago.
        """
        if not self.convergence_read:
            self.convergence_read = True
            self.convergence = mmr_module.load_calibration(
                self.db, self.config["mmr_convergence"]
            )
        return self.convergence

    def _mark_progress(self, rows, match_id):
        """How everybody has moved since you last met them.

        Two local queries and no requests. The rank half works the first time
        it runs, because rank_snapshots has been filling up since long before
        this feature existed; the form half needs a reading taken at an earlier
        meeting, so it appears on the second encounter after that started being
        written. Runs before the form numbers land, so the rank movement is on
        screen while the ACS columns are still filling in.
        """
        puuids = [row.puuid for row in rows if row.puuid and not row.is_self]
        if not puuids:
            return
        progress_module.attach(
            rows,
            self.db.rank_before(puuids, exclude=match_id),
            self.db.form_before(puuids),
            self.db.met_before(puuids, exclude=match_id),
        )
        moved = [row for row in rows if progress_module.anything(row.progress)]
        if moved:
            self.view.update(rows=rows)

    def _mark_parties(self, rows, own_team, match_id):
        """Who came in together, read out of the cache the form numbers filled.

        Runs last because it wants that cache warm, and costs nothing when it
        is not: no requests either way, and a lobby of five solo queues simply
        leaves the column out. Hidden players are included - a group letter is
        not a name, and the lobby already shows which side they are on.
        """
        if not self.config["detect_parties"]:
            return
        puuids = [row.puuid for row in rows]
        groups = party_module.detect(
            rows,
            self.db.party_timeline(puuids, exclude=match_id),
            min_shared=self.config["party_min_shared"],
            own_team=own_team,
            session_gap=self.config["party_session_gap_hours"],
            confirmed=self._confirmed_parties(puuids),
            roster=self._own_roster(),
        )
        party_module.apply(rows, groups)
        if not groups:
            return
        self._append_status(party_module.describe(groups, samples=self.db.team_samples(puuids)))
        self.view.update(rows=rows)

    def _own_roster(self):
        """The puuids of your own party, or () if you queued alone.

        The one party Riot will discuss. Two requests, memoised for the lobby,
        and a failure is silent: this makes the group letters better, it is not
        something the table should stop for. A closed game answers 404 and
        arrives here as None, which is the same as queueing solo.
        """
        try:
            party = self.client.own_party()
        except (ClientUnavailable, requests.RequestException):
            return ()
        return tuple((party or {}).get("members") or ())

    def _confirmed_parties(self, puuids):
        """Pairs the downloaded pool says were in one party, keyed by puuid.

        The pool is keyed by hash and the lobby by puuid, so the two are joined
        here: hash what is on screen, ask the pool about those, translate the
        answer back. This is the only place the two worlds meet, and it is one
        local query - the pool was downloaded between matches.
        """
        if not self.config.get("use_pool"):
            return {}
        by_puuid = pool_module.hashes(self.config, puuids)
        if len(by_puuid) < 2:
            return {}
        back = {digest: puuid for puuid, digest in by_puuid.items()}
        out = {}
        for (left, right), times in self.db.pool_parties(by_puuid.values()).items():
            one, two = back.get(left), back.get(right)
            if one and two:
                out[tuple(sorted((one, two)))] = times
        return out

    def _append_status(self, note):
        """Add one clause to the line under the table, keeping what is there."""
        if not note:
            return
        status = self.view.status
        self.view.update(status=f"{status}; {note}" if status else note)

    def _note_remembered(self, rows):
        """Say how many ranks in the table came out of the local memory."""
        seen = [row for row in rows if row.tier_seen]
        if not seen:
            return
        oldest = min(row.tier_seen for row in seen)[:10]
        plural = "s" if len(seen) != 1 else ""
        self._append_status(
            f"{len(seen)} hidden player{plural} marked ~ carry the last rank "
            f"we saw them at, from {oldest} or later"
        )

    @staticmethod
    def _perf_order(targets, own_team):
        """Enemies first: they are why anyone opens this. You are last."""
        return sorted(targets, key=lambda r: (r.is_self, bool(own_team) and r.team == own_team))

    def _perf_fetcher(self, count):
        """One PerformanceFetcher, built the way both form passes want it.

        Two passes now share it - the visible half of the table and the
        players in Incognito behind it - and they have to agree on the queue,
        the fallback and above all the calibration, because the Score column
        is only comparable across a lobby if every number in it was scored
        against the same population.
        """
        self.calibration = load_calibration(self.db, self.config["rating_calibration"])
        return PerformanceFetcher(
            self.client,
            self.db,
            count=count,
            queue=self.config["performance_queue"],
            fallback=self.config["performance_queue_fallback"],
            calibration=self.calibration,
        )

    def _perf_pass(self, fetcher, targets, count, label):
        """Read form for these rows one at a time, into the table as it lands."""
        for done, row in enumerate(targets, start=1):
            summary = fetcher.for_player(row.puuid)
            row.apply_performance(summary)
            # Write today's reading down before moving on. Next time this
            # player turns up, this is what "they were rated 420 in June" is
            # read out of - the matches behind the number will have scrolled
            # out of the window by then, so there is no other way to have it.
            if summary and not row.is_self:
                self.db.store_form(row.puuid, summary)
            if fetcher.unavailable:
                # Riot has shut match history for other players' puuids. The
                # rest of this lobby will fail identically, and so will the
                # hidden pass and _rank_hidden after it - one latch, so the
                # next stage does not spend a request each finding out again.
                self.history_closed = True
                break
            self.view.update(
                show_perf=True,
                status=f"last {count} matches - {done}/{len(targets)} {label}",
            )

    def _fill_performance(self, targets):
        """Ranks are up already; form costs requests, so it lands row by row."""
        count = self.config["performance_matches"]
        fetcher = self._perf_fetcher(count)
        self._perf_pass(fetcher, targets, count, "players")

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
        self._mark_progress(targets, self.rendered_match)
        self.view.update(
            show_perf=True,
            status=(
                f"form over the last {count} matches - {fetcher.requests_made} requests, "
                f"the rest came from the local cache; {scale}"
            ),
        )

    def _fill_hidden_performance(self, rows, own_team):
        """The form columns for the players in Incognito, from their own matches.

        The gap this closes is the one `_rank_hidden` left open. That stage
        gave a hidden stranger a rank, out of the `competitiveTier` written
        into the record of a match they had already played; this one gives
        them the rest of the row out of the same records - ACS, K/D, HS%,
        KAST, the Score - and hands the rank back a second, deeper chance in
        passing: see `_rank_from_downloaded`. What it does not do is ask the
        one route that *is* a lookup, so the Hidden MMR column stays empty for
        these rows; that band is read out of the RR history, which is a
        question about them now rather than about a match they have finished.

        The principle is the one this program has settled on everywhere else:
        **Incognito closes the live lookup, not a match that is over.** The
        name service will not say who they are and the MMR route will not say
        what they are, and neither is asked. Their match history is a
        different question and Riot answers it about anybody - it is what the
        tracker sites read, and it is what the deep sweep has been reading for
        hidden players since it existed. The live table was the one half of
        the program declining to ask, which made a hidden player a row of
        dashes here and a full row in the deep report over the same lobby.
        The name stays hidden either way: `respect_streamer_mode` is about the
        name, and nothing here prints one.

        Two things keep it honest about cost. It runs *after* the visible half
        of the table is filled, so the five people you can see are never made
        to wait on the five you cannot; and it skips the whole pass when
        `history_closed` is already set, because a route Riot has shut will
        fail identically for every one of them at a request each.
        """
        if not self.config["form_hidden_from_history"] or self.history_closed:
            return
        # `acs is None` and not merely `hidden`: a teammate who was named
        # mid-match by the chat harvest has already been through the pass
        # above, and asking twice is five requests for an answer in hand.
        targets = [r for r in rows if r.hidden and r.puuid and not r.is_self and r.acs is None]
        if not targets:
            return

        count = self.config["performance_matches"]
        fetcher = self._perf_fetcher(count)
        # The line under the table belongs to the pass that just finished, and
        # it is still true; this one adds a clause to it rather than replacing
        # it with a progress counter that will be gone in a second.
        held = self.view.status
        self._perf_pass(fetcher, targets, count, "hidden players")
        self.view.update(status=held)

        self._rank_from_downloaded(targets)

        filled = [row for row in targets if row.acs is not None]
        if not filled:
            return
        plural = "s" if len(filled) != 1 else ""
        self._append_status(
            f"{len(filled)} hidden player{plural} read from their own match records "
            f"- {fetcher.requests_made} requests"
        )
        self.view.update(rows=rows, show_perf=True)

    def _rank_from_downloaded(self, rows):
        """A rank out of the matches the pass above has just downloaded. Free.

        PerformanceFetcher writes a rank snapshot for every player in every
        match it parses - all ten of them, out of a payload it had in hand
        anyway. So by the time the form numbers are on the table, the ranks of
        the people they belong to are sitting in the local database, and this
        is one query rather than a request.

        It is worth the query because `_rank_hidden` gave up earlier and more
        cheaply than this: it reads `rank_hidden_matches` of their competitive
        history and stops at the first match that names a rank, so a player
        whose newest competitive matches are older than that window came back
        empty. The deeper pull the form columns just paid for can answer where
        the shallow one could not - and it also un-marks anybody this session
        wrote down as Unranked on the strength of the shallower look.
        """
        wanted = [row for row in rows if row.puuid and not row.tier]
        if not wanted:
            return
        found = self.db.last_ranks([row.puuid for row in wanted])
        for row in wanted:
            snapshot = found.get(row.puuid)
            if not snapshot:
                continue
            row.tier, row.tier_seen = snapshot["tier"], snapshot["ts"] or ""
            row.unranked = False

    # ------------------------------------------------------------ post-match

    def _post_match(self, match_id, rows, own_team):
        """The scoreboard for the match you just played.

        One request for a match of your own, which also drops the lines of all
        ten players into the cache for free.

        The request only answers once Riot has published the match, which
        happens a moment after the game hands you back to the menus - and
        sometimes a good deal later than a moment. A few tries on the spot, and
        then _defer_summary takes over so the waiting does not hold the poll
        loop up.
        """
        render.info("match over - reading the result...")
        details = fetch_details(
            self.client, match_id, attempts=SUMMARY_TRIES, delay=SUMMARY_TRY_EVERY
        )
        if not details:
            self._defer_summary(match_id, rows, own_team)
            return
        self._summarise(match_id, rows, own_team, details)

    def _defer_summary(self, match_id, rows, own_team):
        """Hand a match Riot has not published yet back to the poll loop.

        Two things happen here, and the second is the one that matters. The
        lobby is kept as it was, so a record that lands in the next minute
        still prints the scoreboard with everything the live table knew; and
        the match is written into the cache, so that a record which takes very
        much longer than that is not lost with it.

        The old path had only the first half, and it ran on a timer:
        summary_wait_seconds of asking and then the match was dropped for good,
        with "the finished match was not published yet - no summary this time".
        That timer is now only the point at which the asking goes quiet. The
        match itself stays queued until it publishes - through the next lobby,
        through closing the program, through a restart - and _resume_summaries
        picks it up from there.
        """
        self.db.queue_summary(match_id, own_team)
        wait = max(0.0, self.config["summary_wait_seconds"])
        now = time.monotonic()
        self.pending_summary = (match_id, rows, own_team)
        self.summary_at = now + SUMMARY_RETRY_EVERY
        self.summary_until = now + wait
        self.summary_quiet = False
        if wait:
            render.info(
                f"the match is not published yet - still asking for it, up to {wait:.0f}s"
            )
        else:
            render.info("the match is not published yet - queued for when it lands")

    def _poll_summary(self, state):
        """The other half of _defer_summary, run from the poll loop.

        It lets go the moment the next lobby opens, because a scoreboard for
        the previous match would print straight over the new one - but letting
        go of the rows is not giving up on the match. That stays in the cache's
        queue, and _resume_summaries takes it from there.
        """
        if self.pending_summary is None:
            return
        match_id, rows, own_team = self.pending_summary
        if state in ("PREGAME", "INGAME"):
            self.pending_summary = None
            render.info("the last match is still unpublished - it stays queued for later")
            return
        now = time.monotonic()
        if now < self.summary_at:
            return
        quick = now < self.summary_until
        if not quick and not self.summary_quiet:
            # Said once, not every five minutes: the queue is doing the waiting
            # now and there is nothing left for anyone to watch.
            self.summary_quiet = True
            render.info(
                "still not published - it stays queued, and will be printed when it lands"
            )
        self.summary_at = now + (SUMMARY_RETRY_EVERY if quick else SUMMARY_SLOW_EVERY)
        self.db.note_summary_try(match_id)
        details = fetch_details(self.client, match_id, attempts=1)
        if not details:
            return
        self.pending_summary = None
        self._summarise(match_id, rows, own_team, details)

    def _resume_summaries(self, state):
        """Matches queued by an earlier lobby, or by an earlier run entirely.

        This is what makes an unpublished match survive being closed: the queue
        lives in the cache, so the next start finds it and asks again. Runs
        between matches only - never over a live lobby, where the requests are
        wanted elsewhere and the scoreboard would have nowhere to go.
        """
        if state in ("PREGAME", "INGAME") or self.pending_summary is not None:
            return
        now = time.monotonic()
        if now < self.resume_at:
            return
        self.resume_at = now + SUMMARY_SLOW_EVERY
        for entry in self.db.pending_summaries():
            self._resume_one(entry)

    def _resume_one(self, entry):
        """Ask once for one queued match, and print it if the record is there."""
        match_id = entry["match_id"]
        if self._summary_expired(entry):
            self.db.drop_summary(match_id)
            render.warn(
                f"match {match_id[:8]} never published in "
                f"{self.config['summary_keep_days']:g} days - dropping it"
            )
            return
        self.db.note_summary_try(match_id)
        details = fetch_details(self.client, match_id, attempts=1)
        if not details:
            return
        rows, own_team = self._rows_from_details(details)
        render.info("a match that was waiting to be published has landed")
        self._summarise(match_id, rows, entry["own_team"] or own_team, details)

    def _summary_expired(self, entry):
        """True once a queued match has been waited on for long enough.

        Riot has been slow; Riot has not been slow for a week. A match that old
        is one that is never coming - a custom, a server that lost it, a match
        id we misread - and the queue should not carry it forever.
        """
        days = self.config["summary_keep_days"]
        if days <= 0:
            return False
        queued = entry.get("queued_at")
        if not queued:
            return False
        try:
            when = datetime.fromisoformat(queued)
        except (TypeError, ValueError):
            return False
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - when).total_seconds() > days * 86400

    def _rows_from_details(self, details):
        """(rows, own team) rebuilt from the record of a match, for a late scoreboard.

        The live rows are long gone by the time a match queued in an earlier run
        finally publishes, and the record carries what the scoreboard actually
        needs: who played, on which side, as whom. Names and ranks are not in it
        - Riot blanks those - but they were never taken from the row either:
        _reveal asks the name service and reads the record, and does that here
        exactly as it does for a match summed up on the spot.
        """
        _, per_player = extract(details)
        known = {puuid: name for puuid, name, _ in self.db.known_names(list(per_player))}
        rows, own_team = [], ""
        for puuid, line in per_player.items():
            agent_id = line.get("agent") or ""
            name = known.get(puuid, "")
            row = PlayerRow(
                puuid=puuid,
                team=line.get("team") or "",
                agent_id=agent_id,
                agent=self.content.agent(agent_id),
                name=name,
                hidden=not name,
                is_self=puuid == self.client.puuid,
            )
            if row.is_self:
                own_team = row.team
                row.name, row.hidden = row.name or "you", False
            rows.append(row)
        return rows, own_team

    def _summarise(self, match_id, rows, own_team, details):
        """Print the scoreboard, now that the record of the match is in hand."""
        # Whichever path got here, this match is no longer owed anything.
        self.db.drop_summary(match_id)
        parsed_id, per_player = extract(details)
        self.db.store_match_perf(parsed_id or match_id, per_player)
        self.db.store_match_meta(parsed_id or match_id, meta(details))
        # Your own line just changed, so the pick advice should not reuse it.
        self.own_lines = None
        for row in rows:
            line = per_player.get(row.puuid)
            row.final = summarise([line]) if line else None

        named, ranked, looked_up = self._reveal(rows, details, parsed_id or match_id)
        # And the band for whoever the live table was not allowed to ask about.
        # Everyone else is carrying theirs from the lobby, so a match where
        # nobody hid costs nothing here at all.
        if self.config["estimate_mmr"]:
            self._read_bands(rows)
        result = outcome(details)
        render.console.print()
        render.console.print(
            render.build_summary_table(
                rows,
                self._result_title(result, own_team),
                own_team=own_team,
                caption=self._caption(match_id, rows, named, ranked, looked_up),
                show_peak=self.config["show_peak_rank"],
                content=self.content,
            )
        )

    def _reveal(self, rows, details, match_id):
        """Name and rank the players who were hidden while the match was played.

        This is what the tracker sites do, and for the same reason: Incognito
        covers the live lookup, not the record of a match that is over. The
        live table above honoured it from the first row to the last.

        Two sources, because Riot moved one of them. The rank is in the record
        itself - `competitiveTier`, filled in for every player whether they hid
        or not, and it is the badge a tracker shows next to someone who spent
        the whole game as [hidden]. The name is not there any more: a
        match-details payload now comes back with gameName and tagLine empty
        for everyone, including you. So the name comes from the name service
        instead, asked again now - Incognito blanks that lookup only for as
        long as you are in a match with the player.

        Both go into the local memory, which is where this pays off: the next
        lobby with that player in it can show what we learned here while they
        are still hiding.

        And once they have a Riot ID again, the ordinary MMR lookup answers
        about them too - which is where the peak and the RR come from, neither
        of which the match record carries. See _reveal_mmr.
        """
        if not self.config["reveal_after_match"]:
            return 0, 0, 0
        named = self._reveal_names(rows, details)
        # Ask Riot first and fall back to the record: the record says what they
        # were when the match started, the lookup says where they are now.
        looked_up = self._reveal_mmr(rows)
        return named, self._reveal_ranks(rows, details, match_id), looked_up

    def _reveal_mmr(self, rows):
        """Current rank, RR and act peak for the players the lobby skipped.

        Incognito covers the MMR lookup exactly the way it covers the name one,
        and the live table honoured that: nobody hidden had a rank read for
        them. The match is over now, so the same request answers - and it
        answers with more than the record does. `competitiveTier` there is the
        tier they were when the game started and nothing else; this is the
        rank they are at now, the RR with it, and the act peak that stands in
        the Peak column for everyone who was never hidden.

        One request per player and only for the ones the live path never asked
        about - usually the two or three who hid, and nobody at all in a lobby
        where everyone was visible. A peak the player chose to hide stays
        hidden here too, the same as it does in the live table.
        """
        targets = [row for row in rows if row.puuid and not row.mmr_read]
        read = 0
        for row in targets:
            payload = self._retry(lambda p=row.puuid: self.client.mmr(p, fresh=True))
            if not payload:
                continue
            parsed = parse_mmr(payload, self.content.current_act)
            row.mmr_read = True
            row.peak_tier = parsed["peak_tier"]
            row.peak_hidden = parsed["peak_hidden"]
            row.wins, row.games = parsed["wins"], parsed["games"]
            if parsed["tier"]:
                # Read for this player today, so it is no longer the rank we
                # happen to remember them at - drop the mark that said so.
                row.tier, row.rr, row.tier_seen = parsed["tier"], parsed["rr"], ""
            read += 1
        return read

    def _reveal_ranks(self, rows, details, match_id):
        """Fill in the rank of everyone the live lobby would not tell us about."""
        found = reveal_tiers(details)
        if not found:
            return 0
        self.db.store_rank_snapshots(
            match_id, {p: t for p, t in found.items() if p != self.client.puuid}
        )
        ranked = 0
        for row in rows:
            tier = found.get(row.puuid)
            # A rank read for the live lobby is the better one: it carries RR
            # and it is current, where this is the rank they were when the
            # match started.
            if not tier or row.tier:
                continue
            row.tier, row.rank_revealed = tier, True
            ranked += 1
        return ranked

    def _resolver(self):
        """The name sources, built on demand for whatever path got here first."""
        if getattr(self, "identity", None) is None:
            self.identity = identity_module.Resolver.build(
                self.config, client=self.client, db=self.db, content=self.content
            )
        return self.identity

    def _reveal_names(self, rows, details):
        """Put Riot IDs to the people who played behind Incognito.

        Every enabled source gets a turn now, in the order config.json listed
        them, and the first one that can name somebody is the one that does.
        The match being over is what makes this different from the live table:
        the name service answers about them again, the record of the match is
        published, and a source that costs fifteen requests is no longer in
        anybody's way.
        """
        resolver = self._resolver()
        missing = [row.puuid for row in rows if row.puuid and (row.hidden or not row.name)]
        if not missing:
            return 0
        try:
            found = resolver.resolve(missing, phase="after", details=details)
        except (ClientUnavailable, requests.RequestException):
            return 0  # the scoreboard is worth printing without the names
        if not found:
            return 0
        revealed = 0
        for row in rows:
            named = found.get(row.puuid)
            if not named:
                continue
            if row.hidden or not row.name:
                row.revealed = True
                revealed += 1
            row.name, row.hidden = named.name, False
            row.name_source, row.name_when = named.source, named.when
        self.named_from = identity_module.describe(
            identity_module.tally(found), resolver.sources
        )
        self.db.remember_found({p: n for p, n in found.items() if p != self.client.puuid})
        return revealed

    def _caption(self, match_id, rows=(), named=0, ranked=0, looked_up=0):
        """The line under the scoreboard: your RR, and where the rest came from."""
        parts = []
        moved = self._rr_caption(match_id)
        if moved:
            parts.append(moved)
        pull = self._own_pull(rows)
        if pull:
            parts.append(pull)
        if named:
            plural = "s" if named != 1 else ""
            source = getattr(self, "named_from", "")
            where = f" ({source})" if source else ""
            parts.append(f"{named} hidden player{plural} named once the match ended{where}")
            self.named_from = ""
        if looked_up:
            plural = "s" if looked_up != 1 else ""
            parts.append(f"{looked_up} rank{plural} and peak{plural} looked up after it")
        if ranked:
            plural = "s" if ranked != 1 else ""
            parts.append(f"{ranked} rank{plural} read from the match record")
        return "   ".join(parts) or None

    def _own_pull(self, rows):
        """Where the system is walking *you*, in one clause under the table.

        The column beside it says the same thing for all ten, and this says it
        again for one player - which is worth the repetition, because the two
        numbers under the scoreboard belong together: what this match paid, and
        what the last twenty say that payment is heading towards. +24 RR is a
        good evening; +24 RR while the system is pulling you a division down is
        a different evening entirely, and only the two of them side by side say
        so.

        Costs nothing: the band is already on the row, read while the lobby was
        on screen.
        """
        for row in rows:
            band = getattr(row, "mmr_band", None)
            if not row.is_self or not band:
                continue
            if not band.placed:
                # Above Immortal there is no rank to name, only the distance.
                return f"hidden rating {band.gap:+.0f} RR of pull"
            where = mmr_module.describe(band, self.content)
            return f"hidden rating {where} ({band.gap:+.0f} RR of pull)"
        return None

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
            if row.name:
                row.name_source = "name-service"
            else:
                # Blank from the name service means the player hid themselves.
                row.hidden = True

        if not respect:
            self._name_hidden(rows)

        wanted = [r for r in rows if not (respect and r.hidden)]
        for done, row in enumerate(wanted, start=1):
            payload = self._retry(lambda p=row.puuid: self.client.mmr(p))
            # Whether it answered decides who the post-match reveal asks about:
            # a call that failed here is worth making again once the match is
            # over, and one that never happened certainly is.
            row.mmr_read = bool(payload)
            parsed = parse_mmr(payload, self.content.current_act)
            row.tier = parsed["tier"] or row.tier
            row.rr = parsed["rr"]
            row.wins = parsed["wins"]
            row.games = parsed["games"]
            row.peak_tier = parsed["peak_tier"]
            # Riot lets players hide their act rank badge - honour that. Your
            # own show_peak_rank is a different thing and is not folded in
            # here: it takes the whole column away rather than marking every
            # player as having withheld something. See render._wanted.
            row.peak_hidden = parsed["peak_hidden"]
            self.view.update(status=f"ranks - {done}/{len(wanted)} players")

        self._remember_ranks(rows, {r.puuid for r in wanted})

        counts = self.db.counts([r.puuid for r in rows if not r.is_self])
        for row in rows:
            row.seen_before = counts.get(row.puuid, 0)

    def _rank_hidden(self, rows):
        """A rank for the players nobody is allowed to look up, out of their matches.

        The gap this closes: somebody in Incognito we have never met before had
        no rank at all. The lookup is the one request we decline to make about
        them, and the local memory only helps if they have stood in one of our
        lobbies before - so a stranger behind [hidden] was a dash, in the one
        column where a dash is worth the most.

        The record of a finished match carries `competitiveTier` for every
        player who was in it, hiding or not. That is the same source the
        post-match reveal already uses and the same badge the tracker sites
        show, and it is not covered by Incognito, which hides a *lookup* rather
        than a match that is over. The name stays hidden either way.

        It reads exactly as far as it has to: their matches newest first,
        stopping at the first one that records a rank, which is almost always
        the first one. Two requests in the ordinary case, none at all when the
        cache has already read that match, and rank_hidden_matches is a ceiling
        for the player who has genuinely never been ranked - they cost their
        whole allowance once and then sit in ranked_matches, read.

        Marked with ~ and dated under the table, exactly like a remembered
        rank, because it is the same kind of claim: what they were when that
        match was played, not what the lookup would say today.
        """
        if not self.config["rank_hidden_from_history"]:
            return
        gaps = [row for row in rows if row.puuid and row.hidden and not row.tier]
        if not gaps:
            return
        # The cache first, and not because _enrich has already tried it: this
        # runs as its own stage now, and a stage that goes to the network for
        # something sitting in a local table is a stage that will one day be
        # reordered into doing it every lobby. One query for the group.
        remembered = self.db.last_ranks([row.puuid for row in gaps])
        still = []
        for row in gaps:
            snapshot = remembered.get(row.puuid)
            if snapshot:
                row.tier, row.tier_seen = snapshot["tier"], snapshot["ts"] or ""
            else:
                still.append(row)
        if not still:
            self.view.update(rows=rows)
            return
        gaps = still
        limit = self.config["rank_hidden_matches"]
        for done, row in enumerate(gaps, start=1):
            self.view.update(
                status=f"rank from match records - {done}/{len(gaps)} hidden players"
            )
            tier, when = self._tier_from_history(row.puuid, limit)
            if tier:
                row.tier, row.tier_seen = tier, when
                # Into the table as it is found, not in a batch at the end:
                # the first hidden rank is worth seeing while the second is
                # still being fetched.
                self.view.update(rows=rows)
            elif not self.history_closed:
                # We read their competitive history and it had no rank in it,
                # which for this column is an answer and not a failure: the
                # account has never been ranked. Printed as "Unranked" rather
                # than as the dash of somebody nobody was allowed to ask about
                # - see render._row_cells. Only when the route was open; a
                # request Riot refused says nothing about the player.
                row.unranked = True
                self.view.update(rows=rows)
            if self.history_closed:
                # Riot has shut the route for other players' puuids: it will
                # fail for all of them, and each failure is a request.
                break
        self.view.update(status="")

    def _tier_from_history(self, puuid, limit):
        """(tier, when) off the newest of their matches that records one."""
        ids = self._history_ids(puuid, limit)
        already = self.db.ranks_read(ids)
        for match_id in ids:
            if match_id in already:
                continue  # read before, and it had nothing for this player
            details = self._history_call(lambda m=match_id: self.client.match_details(m))
            if not details:
                continue
            found = reveal_tiers(details)
            self.db.store_rank_snapshots(
                match_id, {p: t for p, t in found.items() if p != self.client.puuid}
            )
            if found.get(puuid):
                return found[puuid], (meta(details) or {}).get("started_at") or ""
        return 0, ""

    def _history_ids(self, puuid, limit):
        """Their recent *competitive* match ids, newest first.

        Competitive only, and that is the whole economy of this. A match record
        carries `competitiveTier` for every player in it, but outside
        competitive the field is there and is zero - checked against a live
        unrated match, all ten players at tier 0. So the queue fallback that
        the form numbers use would be requests that cannot answer the question
        by construction: a player with no competitive history has no rank to
        find, and the honest cost of learning that is the one request that
        comes back empty.
        """
        payload = self._history_call(
            lambda: self.client.match_history(puuid, limit, "competitive")
        )
        return [
            entry.get("MatchID")
            for entry in (payload or {}).get("History") or []
            if entry.get("MatchID")
        ]

    def _history_call(self, call):
        """One match-history request, with the one failure worth remembering.

        401/403 means Riot has closed the route for other players' puuids -
        the same thing PerformanceFetcher watches for, and for the same reason:
        it will fail identically for the rest of the lobby, so it is worth
        knowing once rather than ten times.
        """
        try:
            return call()
        except ClientUnavailable:
            return None
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else 0
            if status in (401, 403):
                self.history_closed = True
            return None
        except requests.RequestException:
            return None

    # Roughly how often a live match re-reads the chat room for hidden names.
    # The room fills as the match loads and again when it ends, so a single
    # read at the start misses people; this is cheap - two local calls - so it
    # can afford to keep looking.
    HARVEST_EVERY = 15.0

    def reharvest_hidden(self):
        """Re-read the chat room mid-match, throttled, for hidden teammates.

        The one read in show_coregame happens the instant the lobby loads,
        which is often before your team has finished connecting to chat. This
        keeps looking every HARVEST_EVERY seconds so a teammate who shows up
        late - or the all-ten room that opens as the match ends - is still
        caught while the window is open. Costs nothing but two local calls, and
        nothing at all once everyone hidden already has a name.
        """
        if not self.last:
            return
        now = time.monotonic()
        if now - self.harvest_at < self.HARVEST_EVERY:
            return
        self.harvest_at = now
        _match_id, rows, _own_team = self.last
        self._harvest_hidden(rows)

    def _harvest_hidden(self, rows):
        """Write down the names of the hidden players, without showing them.

        This is the quiet half of naming somebody behind Incognito, and the
        reason the post-match reveal became reliable. The client's own chat
        room carries game_name/game_tag for everyone in it - your team for the
        length of the match - and it never learned to hide anybody. But that
        room is only open while the match is on: by the time the scoreboard is
        drawn in menus it has closed, which is why asking only then so often
        found nothing at all.

        So the names are captured now, live, straight into the local memory -
        regardless of respect_streamer_mode, because writing a name down is not
        the same as printing it. The table on screen still honours Incognito to
        the letter (that is _name_hidden's job, and only with the setting off);
        this just makes sure that when the match ends, `memory` has the answer
        the name service will not give.

        Local and free: memory and the chat lists cost no request, and the
        leaderboard is read from whatever dump is already on disk. Enemies are
        not in your chat room, so this mostly rescues hidden teammates; naming
        a hidden enemy still leans on the leaderboard, on having met them
        before, or on an opt-in key source. See identity.py.
        """
        resolver = self._resolver()
        if not resolver:
            return 0
        hidden = [r for r in rows if r.hidden and r.puuid and not r.is_self]
        if not hidden:
            return 0
        try:
            found = resolver.resolve([r.puuid for r in hidden], phase="live")
        except (ClientUnavailable, requests.RequestException):
            return 0
        if found:
            self.db.remember_found(found)
        return len(found)

    def _name_hidden(self, rows):
        """Name the players hiding in the lobby on screen, if we can do it free.

        Only runs with respect_streamer_mode off. Only the live sources are
        asked - the local memory, the client's own chat lists, whatever
        leaderboard dump is already on disk - so a lobby costs no extra
        requests whether it names anybody or not. What the live table must not
        do is sit there waiting on the network while a match starts.

        A name from here is marked in the table, because it is not the same
        claim the name service makes: it is who this puuid was the last time
        something wrote it down.
        """
        if not self.identity:
            return 0
        hidden = [row for row in rows if row.hidden and row.puuid and not row.is_self]
        if not hidden:
            return 0
        try:
            found = self.identity.resolve([row.puuid for row in hidden], phase="live")
        except (ClientUnavailable, requests.RequestException):
            return 0
        for row in hidden:
            named = found.get(row.puuid)
            if not named:
                continue
            row.name, row.hidden = named.name, False
            row.name_source, row.name_when = named.source, named.when
        if found:
            self.db.remember_found(found)
            # Said out loud after the table is drawn, not here: _present clears
            # the status line once the ranks are in.
            self.named_live = identity_module.describe(
                identity_module.tally(found), self.identity.sources
            )
        return len(found)

    def _note_named(self):
        """Say where the names of the people who were hiding came from."""
        if not getattr(self, "named_live", ""):
            return
        self._append_status(f"hidden players named: {self.named_live}")
        self.named_live = ""

    def _remember_ranks(self, rows, asked):
        """Give a player we were not allowed to look up the rank we last saw.

        Somebody playing behind Incognito has no rank in the lobby, because the
        one lookup it could come from is the one we do not make about them. But
        we have met them before, and the record of that match said what they
        were - so the table can say "last seen Immortal 1" instead of a dash.
        It is not their rank today and does not claim to be: the cell is marked
        and the line under the table dates it.

        Costs nothing - one query against snapshots we already collected.
        """
        gaps = [row for row in rows if row.puuid and not row.tier and row.puuid not in asked]
        if not gaps:
            return
        remembered = self.db.last_ranks([row.puuid for row in gaps])
        for row in gaps:
            snapshot = remembered.get(row.puuid)
            if not snapshot:
                continue
            row.tier = snapshot["tier"]
            row.tier_seen = snapshot["ts"] or ""

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
    """Start up, waiting out a content server or a database having a moment.

    Everything the constructor does can fail for a minute and be fine after:
    valorant-api.com serving the agent list, the SQLite file being held by a
    `who` in another window. None of that is worth making someone start the
    program again by hand.
    """
    app = None
    for attempt in range(STARTUP_ATTEMPTS):
        try:
            app = App()
            break
        except Exception as exc:  # noqa: BLE001 - the same rule as the main loop
            render.warn(f"could not start up: {exc.__class__.__name__}: {exc}")
            log_crash(exc)
            if attempt < STARTUP_ATTEMPTS - 1:
                render.info(f"trying again in {STARTUP_RETRY:.0f} seconds", repeat=True)
                time.sleep(STARTUP_RETRY)
    if app is None:
        render.error(f"could not start - the details are in {LOG_PATH.name}")
        return 1
    # Before anything is watched and before any match can start: an update
    # swaps the code out from under a running process, so this is the only
    # moment it is safe to offer one.
    try:
        if update.at_startup(app.config, app.db):
            return 0
    except Exception as exc:  # noqa: BLE001 - a check may never stop a start
        log_crash(exc)

    app.run()
    return 0
