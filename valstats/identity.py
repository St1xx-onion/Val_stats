"""Every way this program knows of putting a Riot ID to a puuid.

Incognito - streamer mode - does one thing: it blanks the name service for as
long as you are in a match with the player. It does not change their puuid,
which is in every lobby payload whether they hide or not, and it does not
reach the other places a Riot ID is written down. So the question is never
"can this puuid be decoded" - it cannot, a puuid is not made out of a name -
but "is this puuid written down anywhere we are already allowed to read".

Eight places are, and each is a Source below:

  memory        encounters.db. We met them before, under their own name, and
                wrote it down. Free, offline, and the source that most often
                answers while the match is still being played.
  party         the people you queued with. Riot will name your own party's
                roster - only yours - and Incognito does not cover the chat
                and friends lists that put names to it. Local, free, and the
                only source that can say why it knows.
  chat          the Riot Client's own chat API. Room participants carry
                game_name/game_tag, and during a match your team is a room;
                the friends list carries them forever. Local, free.
  leaderboard   the act leaderboard: fifteen thousand entries of puuid next to
                gameName, which Riot serves to any client. Only reaches
                Immortal and up, and entries the player anonymised are skipped.
  name-service  the official lookup, asked again once the match is over, when
                it answers about them normally.
  match-record  gameName/tagLine in a match-details payload. Riot blanks these
                now, for everyone including you; kept because it costs nothing
                and the day they put them back it starts working again.
  henrik        api.henrikdev.xyz, which indexes puuid -> current Riot ID.
                Third party, needs a key, off unless you ask for it.
  riot-account  Riot's own account-v1, which answers by puuid. Needs a
                developer key, off unless you ask for it.

The last two are the only ones that send anything about another player off
this machine, which is why neither is in the default order.

Ranked by how current the answer is: the name service and the two key-holding
APIs say who the player is today; the leaderboard says who they were when the
board was last dumped; memory says who they were the day you met. A name
carries its source all the way to the table, so a stale one can be printed as
the weaker claim it is.

None of this is a way around anything. Every source answers to the ordinary
client, over the authorisation the game itself uses, and the one place Riot
lets a player say no to a puuid lookup outright - an anonymised leaderboard
entry - is honoured rather than worked around.
"""

import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import requests

from .client import ClientUnavailable

CACHE_DIR = Path(__file__).resolve().parent.parent / "cache"

# Riot serves the leaderboard a page at a time and caps a page at 1000.
LEADERBOARD_PAGE = 1000

# And the board itself at 15000 - but ask the payload rather than assume it.
LEADERBOARD_CAP = 20000

# Routing value for account-v1, which is regional rather than per-shard.
ACCOUNT_ROUTING = {
    "eu": "europe",
    "na": "americas",
    "latam": "americas",
    "br": "americas",
    "ap": "asia",
    "kr": "asia",
    "pbe": "americas",
}

# Sources in the order they are tried unless config.json says otherwise. Free
# and local first: there is no point paying for an answer already on disk.
# Order is cost, then confidence. "party" sits after the two free ones rather
# than in front of them: it is authoritative and it is the only source that can
# say *why* it knows, but it costs two requests, and a name the local memory
# already has is the same name for nothing.
DEFAULT_SOURCES = ("memory", "chat", "party", "name-service", "match-record", "leaderboard")


@dataclass(frozen=True)
class Named:
    """One answer: the name, where it came from, and how old it is.

    `when` is empty for a source that answers about the player as they are
    now, and an ISO timestamp for one that answers about a moment - the day we
    met them, the hour the leaderboard was dumped. The table reads it as the
    difference between "is" and "was".
    """

    name: str
    source: str
    when: str = ""

    @property
    def live(self):
        return not self.when


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _riot_id(game_name, tag):
    game_name = (game_name or "").strip()
    tag = (tag or "").strip()
    if not game_name:
        return ""
    return f"{game_name}#{tag}" if tag else game_name


# ------------------------------------------------------------------- sources


class Source:
    """One place a Riot ID might be written down.

    `phases` says when it is worth asking. "live" is a lobby on screen, where
    the player is still hiding and an answer has to be free or nearly so;
    "after" is the match being over, or an `identify` run, where a source may
    spend requests.
    """

    key = ""
    label = ""
    phases = ("live", "after")
    # Set by the constructor when the source cannot run: no key, no client, no
    # region. It is then skipped silently, and `identify` says why.
    unavailable = ""

    def lookup(self, puuids, context):
        """{puuid: Named} for whoever this source can name. Never raises."""
        raise NotImplementedError


class Memory(Source):
    """What we wrote down the last time this player was not hiding."""

    key = "memory"
    label = "local memory"

    def __init__(self, db):
        self.db = db
        if not (db and db.conn):
            self.unavailable = "no local memory"

    def lookup(self, puuids, context):
        if self.unavailable:
            return {}
        out = {}
        for puuid, name, seen in self.db.known_names(puuids):
            if name:
                out[puuid] = Named(name, self.key, seen or "")
        return out


class Chat(Source):
    """The client's own chat API, which never learned to hide anybody.

    Two lists, both local and both free. Participants are the people in a chat
    room with you right now - your team for the length of the match, and all
    ten of you in the room that opens when it ends. Friends are everyone on
    the list, whether they are playing or not.

    This is the source that most often names a hidden teammate while the match
    is still being played, because Incognito covers the lobby payload and the
    name service, not the room you are typing in.
    """

    key = "chat"
    label = "client chat"

    def __init__(self, client):
        self.client = client
        if client is None:
            self.unavailable = "no client connection"

    def lookup(self, puuids, context):
        if self.unavailable:
            return {}
        wanted = set(puuids)
        out = {}
        for path, field in (
            ("/chat/v5/participants", "participants"),
            ("/chat/v4/friends", "friends"),
        ):
            try:
                payload = self.client.local("GET", path) or {}
            except (ClientUnavailable, requests.RequestException):
                continue
            for entry in payload.get(field) or []:
                puuid = entry.get("puuid")
                if puuid not in wanted or puuid in out:
                    continue
                name = _riot_id(entry.get("game_name"), entry.get("game_tag"))
                if name:
                    out[puuid] = Named(name, self.key)
        return out


class PartyRoom(Source):
    """Your own party members, named from the lists the client keeps anyway.

    Incognito is a VALORANT setting about what the *match* shows other players.
    It does not reach the Riot account layer, and a party is a Riot account
    thing: you invited these people, you can see them in the party UI, they are
    in a chat room with you and most of them are on your friends list. So a
    party member hiding behind Incognito is hidden from the lobby payload and
    the name service, and not hidden from here.

    This is not a new way of reading a name - Chat already reads those two
    lists. What it adds is the roster: it asks Riot which puuids are actually
    in your party, and only names those. That matters for what the answer
    means. "The friends list happens to know this puuid" and "this puuid is one
    of the five people you queued with, and here is their name" are different
    claims, and only the second is worth putting a mark on in the table.

    Nothing here touches an enemy, or the other four on your side who are not
    in your party. Riot does not discuss those and neither does this.
    """

    key = "party"
    label = "your own party"

    def __init__(self, client):
        self.client = client
        if client is None:
            self.unavailable = "no client connection"

    def lookup(self, puuids, context):
        if self.unavailable:
            return {}
        try:
            party = self.client.own_party()
        except (ClientUnavailable, requests.RequestException):
            return {}
        roster = set((party or {}).get("members") or ())
        # Only the people who are both in your party and being asked about.
        wanted = roster & set(puuids)
        if not wanted:
            return {}

        out = {}
        for path, field in (
            ("/chat/v5/participants", "participants"),
            ("/chat/v4/friends", "friends"),
        ):
            try:
                payload = self.client.local("GET", path) or {}
            except (ClientUnavailable, requests.RequestException):
                continue
            for entry in payload.get(field) or []:
                puuid = entry.get("puuid")
                if puuid not in wanted or puuid in out:
                    continue
                name = _riot_id(entry.get("game_name"), entry.get("game_tag"))
                if name:
                    out[puuid] = Named(name, self.key)
        return out


class NameService(Source):
    """The official lookup, asked once the match it was covering is over.

    Not asked live: the live table already made that call, and the blank it
    came back with for a hidden player is the whole reason this module exists.
    """

    key = "name-service"
    label = "name service"
    phases = ("after",)

    def __init__(self, client):
        self.client = client
        if client is None:
            self.unavailable = "no client connection"

    def lookup(self, puuids, context):
        if self.unavailable:
            return {}
        try:
            # fresh: the blank cached during the match is the one answer that
            # is certainly out of date now.
            found = self.client.names(list(puuids), fresh=True)
        except (ClientUnavailable, requests.RequestException):
            return {}
        return {puuid: Named(name, self.key) for puuid, name in found.items() if name}


class MatchRecord(Source):
    """gameName/tagLine in the record of a finished match.

    Riot blanks both for every player now, so this finds nothing - it is kept
    because it is free, it needs no request at all, and it starts working
    again on its own if they ever put the fields back.
    """

    key = "match-record"
    label = "match record"
    phases = ("after",)

    def lookup(self, puuids, context):
        details = (context or {}).get("details")
        if not details:
            return {}
        wanted = set(puuids)
        out = {}
        for player in details.get("players") or []:
            subject = player.get("subject")
            if subject not in wanted:
                continue
            name = _riot_id(player.get("gameName"), player.get("tagLine"))
            if name:
                out[subject] = Named(name, self.key)
        return out


class Leaderboard(Source):
    """The act leaderboard, dumped once and read out of a file after that.

    Riot publishes the whole Immortal-and-up ladder - fifteen thousand rows of
    puuid next to gameName - to anyone holding a client token. A hidden player
    high enough to be on it is named by it, and the lookup is then a dict hit.

    Fifteen requests to dump, so the freshness rule is lopsided on purpose: a
    live lobby reads whatever file is already there, however old, and never
    spends a request on it; the post-match pass and `identify` re-dump it once
    it has gone stale. A name from here is dated by the dump, not by today.

    Players who anonymised their leaderboard entry are skipped. That is the
    same choice as Incognito, made in the one place Riot honours it against
    everybody, and it is not this program's to undo.
    """

    key = "leaderboard"
    label = "act leaderboard"

    def __init__(self, client, content, max_age_hours=12.0):
        self.client = client
        self.content = content
        self.max_age = max(0.0, float(max_age_hours)) * 3600.0
        # An attribute rather than the constant, so a test can serve a board
        # of six players in pages of two.
        self.page = LEADERBOARD_PAGE
        self._index = None
        self._fetched = ""
        self.act = getattr(content, "current_act", None)
        if client is None:
            self.unavailable = "no client connection"
        elif not self.act:
            self.unavailable = "current act unknown"

    @property
    def path(self):
        return CACHE_DIR / f"leaderboard-{self.client.shard}-{self.client.region}-{self.act}.json"

    def lookup(self, puuids, context):
        if self.unavailable:
            return {}
        index = self.index(refresh=(context or {}).get("phase") != "live")
        out = {}
        for puuid in puuids:
            name = index.get(puuid)
            if name:
                out[puuid] = Named(name, self.key, self._fetched)
        return out

    def index(self, refresh=True):
        """puuid -> Riot ID for the whole board, from disk or from Riot."""
        if self._index is None:
            self._load()
        if refresh and self._stale():
            fetched = self._dump()
            if fetched:
                self._index, self._fetched = fetched, _now()
                self._save()
        return self._index or {}

    def _stale(self):
        if not self._index:
            return True
        if not self.max_age:
            return False
        try:
            return time.time() - self.path.stat().st_mtime > self.max_age
        except OSError:
            return True

    def _load(self):
        self._index, self._fetched = {}, ""
        try:
            blob = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if isinstance(blob, dict) and isinstance(blob.get("players"), dict):
            self._index = blob["players"]
            self._fetched = blob.get("fetched") or ""

    def _save(self):
        try:
            CACHE_DIR.mkdir(exist_ok=True)
            self.path.write_text(
                json.dumps({"fetched": self._fetched, "players": self._index}),
                encoding="utf-8",
            )
        except OSError:
            pass  # a cache we could not write is a slow lookup, not a failure

    def _dump(self, progress=None):
        """The whole board, a page at a time. Empty if Riot would not serve it."""
        found, start, total = {}, 0, None
        while start < LEADERBOARD_CAP:
            url = (
                f"{self.client.pd}/mmr/v1/leaderboards/affinity/{self.client.region}"
                f"/queue/competitive/season/{self.act}"
                f"?startIndex={start}&size={self.page}"
            )
            try:
                page = self.client.remote("GET", url)
            except (ClientUnavailable, requests.RequestException):
                return found
            rows = (page or {}).get("Players") or []
            if total is None:
                total = (page or {}).get("totalPlayers") or 0
            for row in rows:
                # An anonymised entry is a player saying no in the one place
                # Riot asks. Skipped, not worked around.
                if row.get("IsAnonymized"):
                    continue
                name = _riot_id(row.get("gameName"), row.get("tagLine"))
                if name and row.get("puuid"):
                    found[row["puuid"]] = name
            if progress:
                progress(len(found), total or 0)
            if len(rows) < self.page:
                break
            start += self.page
            if total and start >= total:
                break
        return found

    def refresh(self, progress=None):
        """Re-dump the board now, whatever the file's age. Returns how many."""
        if self.unavailable:
            return 0
        fetched = self._dump(progress)
        if not fetched:
            return 0
        self._index, self._fetched = fetched, _now()
        self._save()
        return len(fetched)


class Henrik(Source):
    """api.henrikdev.xyz, which keeps its own puuid -> Riot ID index.

    The one source here that is neither Riot's nor ours: it answers from what
    it scraped, which is why it can still name someone none of the rest will.
    Off unless henrik_api_key is set, because turning it on means sending
    another player's puuid to a third party - the only thing in this program
    that leaves the machine about somebody else.
    """

    key = "henrik"
    label = "henrikdev"
    BASE = "https://api.henrikdev.xyz/valorant/v1/by-puuid/account"

    def __init__(self, api_key, session=None):
        self.api_key = (api_key or "").strip()
        self.session = session or requests.Session()
        if not self.api_key:
            self.unavailable = "no henrik_api_key in config.json"

    def lookup(self, puuids, context):
        if self.unavailable:
            return {}
        out = {}
        for puuid in puuids:
            try:
                resp = self.session.get(
                    f"{self.BASE}/{puuid}",
                    headers={"Authorization": self.api_key},
                    timeout=15,
                )
            except requests.RequestException:
                break  # their service is down; the next puuid will not be luckier
            if resp.status_code == 404:
                continue
            if resp.status_code != 200:
                break  # out of quota, or the key is wrong - stop asking
            try:
                data = (resp.json() or {}).get("data") or {}
            except ValueError:
                continue
            name = _riot_id(data.get("name"), data.get("tag"))
            if name:
                out[puuid] = Named(name, self.key)
        return out


class RiotAccount(Source):
    """Riot's own account-v1, which answers by puuid and never knew about
    Incognito.

    Incognito is a VALORANT setting; this is the account service underneath
    it. Needs a developer key, which is what keeps it off by default: a
    personal key is rate-limited to a trickle and is issued on terms worth
    reading before pointing it at strangers.
    """

    key = "riot-account"
    label = "riot account-v1"

    def __init__(self, api_key, shard, session=None):
        self.api_key = (api_key or "").strip()
        self.routing = ACCOUNT_ROUTING.get((shard or "").lower(), "europe")
        self.session = session or requests.Session()
        if not self.api_key:
            self.unavailable = "no riot_api_key in config.json"

    def lookup(self, puuids, context):
        if self.unavailable:
            return {}
        out = {}
        base = f"https://{self.routing}.api.riotgames.com/riot/account/v1/accounts/by-puuid"
        for puuid in puuids:
            try:
                resp = self.session.get(
                    f"{base}/{puuid}", headers={"X-Riot-Token": self.api_key}, timeout=15
                )
            except requests.RequestException:
                break
            if resp.status_code == 404:
                continue
            if resp.status_code != 200:
                break  # 401/403 is the key, 429 is the quota; both mean stop
            try:
                data = resp.json() or {}
            except ValueError:
                continue
            name = _riot_id(data.get("gameName"), data.get("tagLine"))
            if name:
                out[puuid] = Named(name, self.key)
        return out


BUILDERS = {
    Memory.key: lambda cfg, client, db, content: Memory(db),
    PartyRoom.key: lambda cfg, client, db, content: PartyRoom(client),
    Chat.key: lambda cfg, client, db, content: Chat(client),
    NameService.key: lambda cfg, client, db, content: NameService(client),
    MatchRecord.key: lambda cfg, client, db, content: MatchRecord(),
    Leaderboard.key: lambda cfg, client, db, content: Leaderboard(
        client, content, cfg.get("leaderboard_cache_hours", 12.0)
    ),
    Henrik.key: lambda cfg, client, db, content: Henrik(cfg.get("henrik_api_key")),
    RiotAccount.key: lambda cfg, client, db, content: RiotAccount(
        cfg.get("riot_api_key"), getattr(client, "shard", None)
    ),
}

# Every key config.json will accept in reveal_sources.
KNOWN_SOURCES = tuple(BUILDERS)


# ------------------------------------------------------------------ resolver


class Resolver:
    """The sources config.json asked for, in the order it asked for them.

    A pass stops per player, not per source: the first source that can name
    somebody is the one that names them, and the rest are never asked about
    that puuid. So putting `memory` first is not only an ordering preference -
    it is what keeps a lobby full of people you already know from costing a
    single request.
    """

    def __init__(self, sources):
        self.sources = list(sources)
        self.by_key = {source.key: source for source in self.sources}

    @classmethod
    def build(cls, config, client=None, db=None, content=None, order=None):
        """Assemble from config. A source that cannot run is kept, but marked."""
        wanted = order if order is not None else config.get("reveal_sources") or DEFAULT_SOURCES
        return cls(
            [BUILDERS[key](config, client, db, content) for key in wanted if key in BUILDERS]
        )

    def usable(self, phase):
        return [
            source for source in self.sources if phase in source.phases and not source.unavailable
        ]

    def resolve(self, puuids, phase="after", details=None):
        """{puuid: Named} for as many of them as any enabled source can name."""
        remaining = [puuid for puuid in dict.fromkeys(puuids) if puuid]
        context = {"phase": phase, "details": details}
        found = {}
        for source in self.usable(phase):
            if not remaining:
                break
            try:
                answered = source.lookup(remaining, context) or {}
            except Exception:  # noqa: BLE001 - one bad source must not lose the rest
                answered = {}
            for puuid, named in answered.items():
                if named and named.name and puuid not in found:
                    found[puuid] = named
            remaining = [puuid for puuid in remaining if puuid not in found]
        return found


def tally(found):
    """{source: how many it named}, for the line under the scoreboard."""
    counts = {}
    for named in found.values():
        counts[named.source] = counts.get(named.source, 0) + 1
    return counts


def describe(counts, sources=()):
    """"2 from local memory, 1 from the act leaderboard"."""
    labels = {source.key: source.label for source in sources}
    parts = [
        f"{count} from {labels.get(key, key)}"
        for key, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    ]
    return ", ".join(parts)
