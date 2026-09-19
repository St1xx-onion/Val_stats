"""Talks to the Riot Client the same way the game itself does.

Everything here is plain HTTP:
  * the local client API on 127.0.0.1, authorised with the lockfile password
  * the public pd/glz endpoints, authorised with the tokens the client hands us

No memory reads, no injection, no hooking - nothing Vanguard cares about.

Two things keep the request count down and the pace polite:
  * every outbound call goes through one Pacer, which spaces requests out and
    backs off when Riot answers 429;
  * names, MMR and match history are memoised per player, because agent select
    and the match itself would otherwise ask for the same five allies twice.
"""

import base64
import json
import os
import re
import time
from pathlib import Path

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

LOCKFILE = Path(os.environ["LOCALAPPDATA"]) / "Riot Games/Riot Client/Config/lockfile"
SHOOTER_LOG = Path(os.environ["LOCALAPPDATA"]) / "VALORANT/Saved/Logs/ShooterGame.log"

# Static platform descriptor every Riot client sends. Not a secret and not a
# spoof: it is the exact blob the retail Windows client uses.
CLIENT_PLATFORM = base64.b64encode(
    json.dumps(
        {
            "platformType": "PC",
            "platformOS": "Windows",
            "platformOSVersion": "10.0.19042.1.256.64bit",
            "platformChipset": "Unknown",
        },
        indent=4,
    ).encode()
).decode()

GLZ_RE = re.compile(r"https?://glz-([a-z0-9-]+?)-1\.([a-z0-9]+?)\.a\.pvp\.net")
VERSION_RE = re.compile(r"CI server version:\s*(\S+)")

# How long a fallback state probe stays good for, in seconds.
PROBE_INTERVAL = 5.0

# How often a presence that keeps saying MENUS is checked against the game
# servers. The chat presence is not a contract: it comes back from a chat
# reconnect holding whatever it held before, and it can sit on MENUS through a
# whole match. From the outside that is indistinguishable from a quiet evening
# - which is why it has to be checked rather than believed.
MENUS_VERIFY = 30.0

# How long VALORANT keeps counting as running after its chat presence goes
# quiet. The chat service drops out on its own now and then, and after that the
# presence list holds nothing readable - a match must not become invisible for
# the rest of the session because of it.
PRESENCE_GRACE = 120.0

# How long the last confirmed screen stands while the game servers will not
# say. Long enough to cover a session refresh, a rate-limit backoff or a
# patch-day build string, and short enough that it cannot outlive a match.
STATE_GRACE = 120.0

# Ranked RR history comes a page at a time and Riot will not widen the window.
# Asking for more is not refused, it is answered with an empty list - so this
# is a hard limit rather than a hint, and competitive_updates pages around it.
RR_PAGE = 20

# Tokens last about an hour. Re-reading them well before that costs one local
# request and saves a lobby from collecting 401s halfway through.
TOKEN_TTL = 45 * 60

# Memoised answers are one per player per lookup; a session that runs for days
# meets a lot of players.
MAX_MEMO = 1024

# ShooterGame.log grows into the tens of megabytes. Read the tail first and
# only fall back to the whole file if what we are after is not in it.
LOG_TAIL_BYTES = 1 << 20

# How many times one request is worth re-sending when Riot rate-limits us.
RETRIES = 3
MAX_BACKOFF = 20.0

# Riot's item type for agents, used by the store entitlements endpoint.
AGENT_ITEM_TYPE = "01bb38e1-da47-4e6a-9b3d-945fe4655707"

# Handed to every account and never listed as an entitlement.
STARTER_AGENTS = {
    "9f0d8ba9-4140-b941-57d3-a7ad57c6b417",  # Brimstone
    "add6443a-41bd-e414-f6ad-e58d267f4e95",  # Jett
    "eb93336a-449b-9c1b-0a54-a891f7921d69",  # Phoenix
    "569fdd95-4d10-43ab-ca70-79becc718b46",  # Sage
    "320b2a48-4d9b-a075-30f1-1f93a9b638fa",  # Sova
}

_MISS = object()


def decode_private(private):
    """The base64 JSON blob a presence carries, or None if there is none."""
    if not private:
        return None
    try:
        return json.loads(base64.b64decode(private))
    except (ValueError, TypeError):
        return None


def presence_state(presences, puuid):
    """(session loop state, is VALORANT running) for one player.

    A logged-in account has several presences at once - the Riot Client
    publishes one of its own next to VALORANT's, and it carries no loop state
    at all. Read every entry that belongs to us and let VALORANT's win, rather
    than trusting whichever one happens to come first.
    """
    state = None
    game_running = False
    for presence in presences:
        if presence.get("puuid") != puuid:
            continue
        is_valorant = (presence.get("product") or "").lower() == "valorant"
        game_running = game_running or is_valorant
        loop = (decode_private(presence.get("private")) or {}).get("sessionLoopState")
        if not loop:
            continue
        if is_valorant:
            return loop, True
        state = state or loop
    return state, game_running


def retry_delay(response, attempt):
    """How long to wait after a 429: what Riot asked for, else a backoff."""
    header = None
    if response is not None:
        header = (response.headers or {}).get("Retry-After")
    try:
        if header is not None:
            return max(0.0, min(MAX_BACKOFF, float(header)))
    except (TypeError, ValueError):
        pass
    return min(MAX_BACKOFF, 2.0 * (2**attempt))


def _json(response):
    """The body as JSON, or None. A truncated answer is not worth a traceback."""
    if not response.content:
        return None
    try:
        return response.json()
    except ValueError:
        return None


class Pacer:
    """One shared throttle for every outbound request."""

    def __init__(self, gap=0.15):
        self.gap = max(0.0, float(gap))
        self._next_at = 0.0

    def wait(self):
        now = time.monotonic()
        if now < self._next_at:
            time.sleep(self._next_at - now)
        self._next_at = max(now, self._next_at) + self.gap

    def penalise(self, seconds):
        """Push everything back - a 429 is about the account, not one call."""
        self._next_at = max(self._next_at, time.monotonic() + seconds)


class ClientUnavailable(RuntimeError):
    """Riot Client is not running, or not far enough into startup yet."""


class Client:
    def __init__(self, config):
        self.config = config
        self.session = requests.Session()
        self.session.verify = False
        self.local_port = None
        self.local_auth = None
        self.puuid = None
        self.access_token = None
        self.entitlement = None
        self.region = config.get("region")
        self.shard = config.get("shard")
        self.client_version = config.get("client_version")
        self.pacer = Pacer(config.get("request_gap", 0.15))
        self.requests_made = 0
        self.rate_limited = 0
        self.menus_verify = max(0.0, float(config.get("menus_verify", MENUS_VERIFY)))
        self._probe_at = 0.0
        self._probe_result = None
        self._tokens_at = 0.0
        self._game_seen_at = 0.0
        self._menus_checked_at = 0.0
        # The last screen the game or its servers actually confirmed, and when.
        # What _held stands on while the servers will not answer.
        self._state = None
        self._state_at = 0.0
        # Cleared the moment the chat presence is caught claiming MENUS during
        # a match; restored when it agrees with the game servers again.
        self._trust_presence = True
        # Counters worth reporting: they are what "it said I was in menus"
        # looks like from in here.
        self.presence_stale = 0
        self.version_refreshed = 0
        # Probes the game servers would not answer. Counted rather than
        # swallowed: this is what "both windows said I was in menus" looked
        # like from in here, and it used to leave no trace at all.
        self.probe_failed = 0
        self._memo = {}
        self._memo_ttl = 60.0 * float(config.get("player_cache_minutes", 20))

    # ------------------------------------------------------------------ setup

    def connect(self):
        """Read the lockfile and pull fresh tokens. Safe to call repeatedly."""
        self._read_lockfile()
        self._read_tokens()
        self._detect_region()
        self._detect_version()
        return self

    def _read_lockfile(self):
        if not LOCKFILE.is_file():
            raise ClientUnavailable("lockfile not found - is the Riot Client running?")
        try:
            raw = LOCKFILE.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ClientUnavailable(f"cannot read lockfile: {exc}") from exc
        parts = raw.split(":")
        if len(parts) < 5:
            raise ClientUnavailable(f"malformed lockfile: {raw!r}")
        _name, _pid, port, password, _protocol = parts[:5]
        self.local_port = port
        self.local_auth = base64.b64encode(f"riot:{password}".encode()).decode()

    def _read_tokens(self):
        ent = self.local("GET", "/entitlements/v1/token")
        if not ent or not ent.get("accessToken"):
            raise ClientUnavailable("client has no session yet - log in first")
        self.access_token = ent["accessToken"]
        self.entitlement = ent["token"]
        self.puuid = ent["subject"]
        self._tokens_at = time.monotonic()

    def keep_fresh(self):
        """Re-read the tokens before they age out, rather than after.

        Riot's access token lives about an hour, and a lobby that opens on a
        stale one spends its first requests collecting 401s and retrying them.
        One local call every three quarters of an hour is cheaper than that.
        """
        if self._tokens_at and time.monotonic() - self._tokens_at < TOKEN_TTL:
            return False
        self._read_tokens()
        return True

    def _detect_region(self):
        if self.region and self.shard:
            return
        match = self._search_log(GLZ_RE)
        if match is None:
            raise ClientUnavailable(
                "could not read ShooterGame.log to detect your region; "
                "set region and shard in config.json manually"
            )
        if not match:
            raise ClientUnavailable(
                "region not found in ShooterGame.log - launch VALORANT once, "
                "or set region/shard in config.json"
            )
        self.region, self.shard = match.group(1), match.group(2)

    def _detect_version(self):
        if self.client_version:
            return
        match = self._search_log(VERSION_RE)
        if match:
            self.client_version = match.group(1)
            return
        # Fall back to the community mirror of the current build string.
        try:
            resp = requests.get("https://valorant-api.com/v1/version", timeout=10)
            resp.raise_for_status()
            self.client_version = resp.json()["data"]["riotClientVersion"]
        except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
            # Nothing works without a build string, but this is worth another
            # try in five seconds rather than a traceback.
            raise ClientUnavailable(f"could not read the client build string: {exc}") from exc

    def _refresh_version(self):
        """Re-read the build string from the log; True if it really changed.

        Only worth doing when Riot has started refusing what we send: a version
        pinned by hand in config.json is left alone, because second-guessing it
        helps nobody who went to the trouble of setting it.
        """
        if self.config.get("client_version"):
            return False
        match = self._search_log(VERSION_RE)
        fresh = match.group(1) if match else None
        if not fresh or fresh == self.client_version:
            return False
        self.client_version = fresh
        self.version_refreshed += 1
        return True

    @staticmethod
    def _search_log(pattern):
        """Match in ShooterGame.log; None means the log itself is unreadable.

        The region line is written at startup and the file grows into the tens
        of megabytes, so try the cheap tail first and only then pay for all of it.
        """
        try:
            size = SHOOTER_LOG.stat().st_size
            with SHOOTER_LOG.open("r", encoding="utf-8", errors="ignore") as handle:
                if size > LOG_TAIL_BYTES:
                    handle.seek(size - LOG_TAIL_BYTES)
                    found = pattern.search(handle.read())
                    if found:
                        return found
                    handle.seek(0)
                return pattern.search(handle.read())
        except OSError:
            return None

    # ------------------------------------------------------------------- urls

    @property
    def pd(self):
        return f"https://pd.{self.shard}.a.pvp.net"

    @property
    def glz(self):
        return f"https://glz-{self.region}-1.{self.shard}.a.pvp.net"

    def _headers(self):
        return {
            "Authorization": f"Bearer {self.access_token}",
            "X-Riot-Entitlements-JWT": self.entitlement,
            "X-Riot-ClientPlatform": CLIENT_PLATFORM,
            "X-Riot-ClientVersion": self.client_version,
        }

    # ------------------------------------------------------------------- memo

    def _memo_get(self, key):
        entry = self._memo.get(key)
        if entry is None:
            return _MISS
        expires, value = entry
        if time.monotonic() >= expires:
            del self._memo[key]
            return _MISS
        return value

    def _memo_set(self, key, value):
        if self._memo_ttl <= 0:
            return
        if len(self._memo) >= MAX_MEMO:
            self._prune_memo()
        self._memo[key] = (time.monotonic() + self._memo_ttl, value)

    def _prune_memo(self):
        """Expired entries are only dropped when read, and this runs for days."""
        now = time.monotonic()
        self._memo = {key: entry for key, entry in self._memo.items() if entry[0] > now}
        if len(self._memo) >= MAX_MEMO:
            # More live entries than that means the TTL is long and the session
            # longer. Starting over costs a few requests, not correctness.
            self._memo.clear()

    def forget(self, *keys):
        """Drop memoised answers - used when we need a genuinely fresh one."""
        for key in keys:
            self._memo.pop(key, None)

    # ---------------------------------------------------------------- request

    def local(self, method, path, **kwargs):
        """One call to the client's own API.

        Anything wrong here means the client we hold a lockfile for is gone or
        not itself, so every failure comes back as ClientUnavailable - which is
        the one thing the caller already knows how to recover from.
        """
        if not self.local_port:
            raise ClientUnavailable("not connected")
        try:
            resp = self.session.request(
                method,
                f"https://127.0.0.1:{self.local_port}{path}",
                headers={"Authorization": f"Basic {self.local_auth}"},
                timeout=10,
                **kwargs,
            )
        except requests.RequestException as exc:
            raise ClientUnavailable(f"local API unreachable: {exc}") from exc
        if resp.status_code == 404:
            return None
        if resp.status_code in (401, 403):
            raise ClientUnavailable("local API refused our password - the client restarted")
        if resp.status_code >= 400:
            raise ClientUnavailable(f"local API answered {resp.status_code} for {path}")
        return _json(resp)

    def remote(self, method, url, strict=False, **kwargs):
        """One authenticated call to pd/glz, paced and retried where it helps.

        `strict` is for the callers that have to tell "Riot says no" apart from
        "Riot would not answer". Normally both come back as None, which is
        right for a lookup - a player with no match and a player we could not
        ask about are equally unprintable. It is exactly wrong for the state
        probe, where None means "you are in menus", and a build string a patch
        had just invalidated therefore read as forty minutes of menus in the
        middle of a match. Under `strict` only a real 404 is None; anything we
        could not get an answer out of raises.
        """
        resp = None
        refreshed = False
        rebuilt = False
        attempt = 0
        while attempt < RETRIES:
            self.pacer.wait()
            self.requests_made += 1
            resp = self.session.request(method, url, headers=self._headers(), timeout=15, **kwargs)

            if resp.status_code == 429:
                self.rate_limited += 1
                self.pacer.penalise(retry_delay(resp, attempt))
                attempt += 1
                continue
            if resp.status_code == 404:
                return None
            if resp.status_code == 400:
                # This is what a VALORANT update looks like from down here: the
                # build string we read when we connected stopped being accepted
                # and every call answers 400, which reads all the way up as "no
                # match, no lobby, nothing happening" - for the rest of the day.
                # The log is rewritten when the game next starts, so the answer
                # is usually already on disk.
                if not rebuilt and self._refresh_version():
                    rebuilt = True
                    continue
                if strict:
                    raise ClientUnavailable(
                        "Riot refused our build string - VALORANT has probably patched"
                    )
                return None
            if resp.status_code == 401:
                # Tokens live about an hour. Refreshing and re-sending is free
                # of charge to the caller; a second 401 means the session went.
                if refreshed:
                    raise ClientUnavailable("tokens were refused even after a refresh")
                refreshed = True
                self._read_tokens()
                continue
            if resp.status_code >= 500 and attempt < RETRIES - 1:
                self.pacer.penalise(min(MAX_BACKOFF, 1.0 * (2**attempt)))
                attempt += 1
                continue
            resp.raise_for_status()
            return _json(resp)

        resp.raise_for_status()  # out of retries: let the caller see the 429
        return None

    # ------------------------------------------------------------------ state

    def session_state(self):
        """MENUS / PREGAME / INGAME, or None while the client is still loading.

        The chat presence leads: it answers instantly and costs one local
        request. But it is a convenience, not a contract, and it fails in two
        ways that look identical from the outside - a lobby that never appears.
        It disappears, because the chat service dropped out and took our own
        entry with it; and it goes stale, because it came back from that
        reconnect holding MENUS and will hold it for the rest of the evening.

        So neither silence nor MENUS is taken on trust for long. Both get
        checked against pregame/core-game, which cannot be stale - and once the
        presence has been caught out, the game servers lead until it agrees
        with them again.

        And a presence with no loop state in it at all is an ordinary Tuesday
        rather than a fault: a 2v2 skirmish publishes one that carries the
        party and the queue id and nothing about which screen the game is on.
        For that whole queue the game servers are the only thing that knows,
        which is why a probe that cannot answer must say so instead of
        guessing - see _probe_state and _held.
        """
        state, game_running = self._presence_state()
        now = time.monotonic()
        if game_running:
            self._game_seen_at = now
        running = game_running or (
            self._game_seen_at and now - self._game_seen_at < PRESENCE_GRACE
        )
        if state and state != "MENUS":
            # Only MENUS is worth verifying. A stale PREGAME or INGAME gives
            # itself away within seconds anyway, because the lobby behind it
            # does not load - and that path already retries and moves on.
            self._trust_presence = True
            return self._held(state)
        if not running:
            # No sign of the game anywhere: nothing to miss, and nothing the
            # game servers would answer about it either. The last screen we
            # knew is forgotten here rather than held - the game is shut, and
            # a remembered INGAME would outlive it by the whole grace period.
            self._trust_presence = True
            self._state = None
            self._state_at = 0.0
            return state or None
        if state == "MENUS":
            return self._held(self._verify_menus(now))
        return self._held(self._probe_state())

    def _held(self, state):
        """Remember a screen we are sure of; stand on it when we stop being.

        The rule, and it is the whole of the fix: **"we could not ask" is not
        "you are in menus"**. An unanswerable probe comes back as None now, and
        None here means "the last screen we actually knew, while that is still
        recent enough to mean anything" - and after that, "no idea", which at
        least reads as one on screen instead of as a fact about menus.
        """
        if state is not None:
            self._state = state
            self._state_at = time.monotonic()
            return state
        if self._state_at and time.monotonic() - self._state_at < STATE_GRACE:
            return self._state
        return None

    def _verify_menus(self, now):
        """Check a presence that says MENUS against the game servers.

        Two requests, and while the presence looks healthy only one pair every
        menus_verify seconds - the standing cost of noticing that it has gone
        quiet about a match. A probe that cannot reach Riot proves nothing, so
        the presence keeps its answer until one does.
        """
        if not self.menus_verify:
            return "MENUS"
        if self._trust_presence and now - self._menus_checked_at < self.menus_verify:
            return "MENUS"
        self._menus_checked_at = now
        probed = self._probe_state()
        if probed is None:
            return "MENUS"
        if probed == "MENUS":
            self._trust_presence = True
            return "MENUS"
        if self._trust_presence:
            self.presence_stale += 1
        self._trust_presence = False
        return probed

    def _presence_state(self):
        """(loop state, is VALORANT running) from our own chat presences."""
        data = self.local("GET", "/chat/v4/presences")
        if not data:
            return None, False
        return presence_state(data.get("presences") or [], self.puuid)

    def _probe_state(self, max_age=PROBE_INTERVAL):
        """The state according to the game servers, or None if they would not say.

        Two requests, so the answer is held for a few seconds between polls.

        The None is the whole point and it used to be a "MENUS". This function
        began by assuming menus and then looked for a match to contradict it,
        which meant every way of failing to reach Riot - an expired session, a
        429 out of retries, a build string a patch had just invalidated -
        arrived at the caller as a confident *fact* that the player was sitting
        in menus. Nothing ever corrected it, because nothing knew a question
        had gone unanswered. Two windows left open would both sit there saying
        "still watching - in menus" through an entire match.

        Now "no match" and "no answer" are different returns, and only the
        first is menus. `strict` is what separates them - see remote().
        """
        now = time.monotonic()
        if now - self._probe_at < max_age:
            return self._probe_result
        self._probe_at = now

        state = "MENUS"
        try:
            if (
                self.remote(
                    "GET", f"{self.glz}/core-game/v1/players/{self.puuid}", strict=True
                )
                or {}
            ).get("MatchID"):
                state = "INGAME"
            elif (
                self.remote("GET", f"{self.glz}/pregame/v1/players/{self.puuid}", strict=True)
                or {}
            ).get("MatchID"):
                state = "PREGAME"
        except (ClientUnavailable, requests.RequestException):
            self.probe_failed += 1
            # Thrown away rather than kept: the few seconds of cache after this
            # would otherwise hand back an answer from before the failure, which
            # is the same stale-fact bug one layer along. None is the honest
            # value and _held knows what to do with it.
            self._probe_result = None
            # The commonest reason the game servers stop answering is a session
            # that aged out, and re-reading the tokens is one local request that
            # fixes it. Asking for it here rather than doing it here: both loops
            # call keep_fresh() every poll anyway, and a probe is not the place
            # to rebuild a connection.
            self._tokens_at = 0.0
            return None
        self._probe_result = state
        return state

    # ------------------------------------------------------------------- game

    def pregame_match(self, match_id=None):
        """The agent-select lobby. Pass a known id to skip the lookup request."""
        if not match_id:
            player = self.remote("GET", f"{self.glz}/pregame/v1/players/{self.puuid}")
            match_id = (player or {}).get("MatchID")
        if not match_id:
            return None
        return self.remote("GET", f"{self.glz}/pregame/v1/matches/{match_id}")

    def coregame_match(self):
        player = self.remote("GET", f"{self.glz}/core-game/v1/players/{self.puuid}")
        match_id = (player or {}).get("MatchID")
        if not match_id:
            return None
        return self.remote("GET", f"{self.glz}/core-game/v1/matches/{match_id}")

    def coregame_loadouts(self, match_id):
        return self.remote("GET", f"{self.glz}/core-game/v1/matches/{match_id}/loadouts")

    def own_party(self):
        """The party you queued in: {puuid, ...} for its members, or None.

        This is the one party Riot will talk about. It says nothing about the
        other four people on your side and nothing at all about the enemy -
        which is exactly why party.py exists - but about your own five it is
        not a guess, it is the answer, and it is free and local to the game
        servers.

        Two requests per lobby, memoised, and only when a lobby is on screen. None when there is no
        party, which is what the game says when it is not running: the route
        answers 404 RESOURCE_NOT_FOUND rather than erroring, and remote() turns
        that into None, so a closed game costs nothing and warns about nothing.

        Note what is *not* here: names. A party member carries Subject and
        cosmetics and no Riot ID at all, so this identifies nobody on its own -
        it says who is with whom, which is a different question. See
        identity.PartyRoom for the half that puts names to them.
        """
        # Memoised for the same reason names and MMR are: the lobby asks once
        # for the group letters and the reveal asks again for the names, and
        # the answer cannot change between those two moments. Without this the
        # party costs four requests a lobby instead of two.
        cached = self._memo_get("own-party")
        if cached is not _MISS:
            return cached

        player = self.remote("GET", f"{self.glz}/parties/v1/players/{self.puuid}")
        party_id = (player or {}).get("CurrentPartyID")
        if not party_id:
            self._memo_set("own-party", None)
            return None
        party = self.remote("GET", f"{self.glz}/parties/v1/parties/{party_id}")
        if not party:
            self._memo_set("own-party", None)
            return None
        members = []
        for member in party.get("Members") or []:
            subject = member.get("Subject") or (member.get("PlayerIdentity") or {}).get("Subject")
            if subject:
                members.append(subject)
        if not members:
            self._memo_set("own-party", None)
            return None
        got = {"id": party_id, "members": sorted(set(members))}
        self._memo_set("own-party", got)
        return got

    # ----------------------------------------------------------------- player

    def names(self, puuids, fresh=False):
        """puuid -> Name#Tag. Hidden (streamer mode) players come back blank.

        Incognito blanks a player only for as long as you are in a match with
        them; afterwards the same lookup answers normally. `fresh` is for
        exactly that moment - the blank we cached during the match is the one
        answer we must not reuse once it is over.
        """
        out = {}
        missing = []
        if fresh:
            self.forget(*(("name", puuid) for puuid in puuids))
        for puuid in puuids:
            cached = self._memo_get(("name", puuid))
            if cached is _MISS:
                missing.append(puuid)
            else:
                out[puuid] = cached
        if not missing:
            return out

        data = self.remote("PUT", f"{self.pd}/name-service/v2/players", json=missing)
        for entry in data or []:
            game_name = entry.get("GameName") or ""
            tag = entry.get("TagLine") or ""
            value = f"{game_name}#{tag}" if game_name else ""
            subject = entry.get("Subject")
            out[subject] = value
            self._memo_set(("name", subject), value)
        return out

    def mmr(self, puuid, fresh=False):
        """Cached for the session; `fresh` is for reading your own RR after a match."""
        key = ("mmr", puuid)
        if fresh:
            self.forget(key)
        else:
            cached = self._memo_get(key)
            if cached is not _MISS:
                return cached
        payload = self.remote("GET", f"{self.pd}/mmr/v1/players/{puuid}")
        self._memo_set(key, payload)
        return payload

    def match_history(self, puuid, count=5, queue="competitive", start=0):
        key = ("history", puuid, count, queue, start)
        cached = self._memo_get(key)
        if cached is not _MISS:
            return cached
        url = (
            f"{self.pd}/match-history/v1/history/{puuid}"
            f"?startIndex={start}&endIndex={start + count}"
        )
        if queue:
            url += f"&queue={queue}"
        payload = self.remote("GET", url)
        self._memo_set(key, payload)
        return payload

    def competitive_updates(self, puuid, count=20):
        """What each of a player's recent ranked matches paid, newest first.

        This is the one route that carries RR per match rather than RR right
        now, which is what makes the hidden rating readable at all: see mmr.py
        for what is done with it. It answers for other players' puuids as well
        as your own - checked, not assumed - and a shard that has closed it
        comes back through remote() as None rather than as an exception, so a
        caller that loses it simply has no estimate to show.

        Asking for more than RR_PAGE at once does not fail, which is the trap:
        it comes back 200 OK with an empty Matches list, so a window of 30 reads
        as "this player has never played ranked" rather than as an error. Hence
        the paging - and hence asking for forty matches costs two requests and
        returns forty, instead of costing one and returning nothing.

        Memoised per page for the session: a match's RR is fixed once the match
        ends, so asking twice in one lobby can only get the same answer.
        """
        wanted = max(1, int(count))
        out = []
        for start in range(0, wanted, RR_PAGE):
            page = self._rr_page(puuid, start, min(RR_PAGE, wanted - start))
            out.extend(page)
            if len(page) < min(RR_PAGE, wanted - start):
                break  # that was the end of their history
        return out[:wanted]

    def _rr_page(self, puuid, start, count):
        key = ("rr", puuid, start, count)
        cached = self._memo_get(key)
        if cached is not _MISS:
            return cached
        url = (
            f"{self.pd}/mmr/v1/players/{puuid}/competitiveupdates"
            f"?startIndex={start}&endIndex={start + count}&queue=competitive"
        )
        payload = self.remote("GET", url)
        matches = (payload or {}).get("Matches") or []
        self._memo_set(key, matches)
        return matches

    def match_details(self, match_id):
        return self.remote("GET", f"{self.pd}/match-details/v1/matches/{match_id}")

    def owned_agents(self):
        """Agent uuids this account can actually pick, or None if unreadable.

        One request for your own entitlements, cached for the session. Riot
        grants the five starter agents outright and does not list them here,
        so they are added back by hand.
        """
        key = ("owned", self.puuid)
        cached = self._memo_get(key)
        if cached is not _MISS:
            return cached
        try:
            payload = self.remote(
                "GET", f"{self.pd}/store/v1/entitlements/{self.puuid}/{AGENT_ITEM_TYPE}"
            )
        except (ClientUnavailable, requests.RequestException):
            return None
        entitlements = (payload or {}).get("Entitlements") or []
        owned = {(e.get("ItemID") or "").lower() for e in entitlements if e.get("ItemID")}
        if not owned:
            # Nothing came back: better to advise from every agent than from none.
            return None
        owned |= STARTER_AGENTS
        self._memo_set(key, owned)
        return owned
