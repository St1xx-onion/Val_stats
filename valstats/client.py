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

# ShooterGame.log grows into the tens of megabytes. Read the tail first and
# only fall back to the whole file if what we are after is not in it.
LOG_TAIL_BYTES = 1 << 20

# How many times one request is worth re-sending when Riot rate-limits us.
RETRIES = 3
MAX_BACKOFF = 20.0

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
        self._probe_at = 0.0
        self._probe_result = None
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
        resp = requests.get("https://valorant-api.com/v1/version", timeout=10)
        resp.raise_for_status()
        self.client_version = resp.json()["data"]["riotClientVersion"]

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
        if self._memo_ttl > 0:
            self._memo[key] = (time.monotonic() + self._memo_ttl, value)

    def forget(self, *keys):
        """Drop memoised answers - used when we need a genuinely fresh one."""
        for key in keys:
            self._memo.pop(key, None)

    # ---------------------------------------------------------------- request

    def local(self, method, path, **kwargs):
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
        resp.raise_for_status()
        return resp.json() if resp.content else None

    def remote(self, method, url, **kwargs):
        """One authenticated call to pd/glz, paced and retried where it helps."""
        resp = None
        for attempt in range(RETRIES):
            self.pacer.wait()
            self.requests_made += 1
            resp = self.session.request(method, url, headers=self._headers(), timeout=15, **kwargs)

            if resp.status_code == 429:
                self.rate_limited += 1
                self.pacer.penalise(retry_delay(resp, attempt))
                continue
            if resp.status_code in (400, 404):
                return None
            if resp.status_code == 401:
                # Tokens live about an hour; refresh and let the caller retry.
                self._read_tokens()
                raise ClientUnavailable("tokens expired and were refreshed")
            if resp.status_code >= 500 and attempt < RETRIES - 1:
                self.pacer.penalise(min(MAX_BACKOFF, 1.0 * (2**attempt)))
                continue
            resp.raise_for_status()
            return resp.json() if resp.content else None

        resp.raise_for_status()  # out of retries: let the caller see the 429
        return None

    # ------------------------------------------------------------------ state

    def session_state(self):
        """MENUS / PREGAME / INGAME, or None while the client is still loading."""
        state, game_running = self._presence_state()
        if state:
            return state
        # The chat presence is a convenience, not a contract: the Riot Client
        # publishes its own entry alongside VALORANT's and it carries no loop
        # state at all. If the game is up but said nothing we can read, ask the
        # game servers directly instead of pretending VALORANT is not running.
        if game_running:
            return self._probe_state()
        return None

    def _presence_state(self):
        """(loop state, is VALORANT running) from our own chat presences."""
        data = self.local("GET", "/chat/v4/presences")
        if not data:
            return None, False
        return presence_state(data.get("presences") or [], self.puuid)

    def _probe_state(self):
        """Fallback: derive the state from the pregame/core-game endpoints.

        Two requests, so the answer is held for a few seconds between polls.
        """
        now = time.monotonic()
        if now - self._probe_at < PROBE_INTERVAL:
            return self._probe_result
        self._probe_at = now

        state = "MENUS"
        try:
            if (self.remote("GET", f"{self.glz}/core-game/v1/players/{self.puuid}") or {}).get(
                "MatchID"
            ):
                state = "INGAME"
            elif (self.remote("GET", f"{self.glz}/pregame/v1/players/{self.puuid}") or {}).get(
                "MatchID"
            ):
                state = "PREGAME"
        except ClientUnavailable:
            return self._probe_result  # tokens were refreshed; try again next poll
        self._probe_result = state
        return state

    # ------------------------------------------------------------------- game

    def pregame_match(self):
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

    # ----------------------------------------------------------------- player

    def names(self, puuids):
        """puuid -> Name#Tag. Hidden (streamer mode) players come back blank."""
        out = {}
        missing = []
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

    def match_details(self, match_id):
        return self.remote("GET", f"{self.pd}/match-details/v1/matches/{match_id}")
