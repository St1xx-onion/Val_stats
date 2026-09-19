"""Local memory: who you have run into before, and their per-match numbers.

Everything stays in a SQLite file next to the script. Nothing is uploaded.

Match results never change once the match ends, so every match we pull apart is
cached here forever. A pulled match stores the line for all ten players in it,
not just the one we were asking about - so the longer you run this, the fewer
requests each lobby costs.

What accumulates here also feeds two things the live table cannot: the `who`
and `top` commands, and the population the 0-1000 score calibrates against.
"""

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from . import conduct as conduct_module

DB_PATH = Path(__file__).resolve().parent.parent / "encounters.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS encounters (
    puuid      TEXT PRIMARY KEY,
    name       TEXT,
    times      INTEGER NOT NULL DEFAULT 0,
    last_seen  TEXT,
    first_seen TEXT
);
CREATE TABLE IF NOT EXISTS seen_matches (
    match_id TEXT PRIMARY KEY,
    ts       TEXT
);
CREATE TABLE IF NOT EXISTS match_perf (
    match_id     TEXT NOT NULL,
    puuid        TEXT NOT NULL,
    score        INTEGER NOT NULL DEFAULT 0,
    rounds       INTEGER NOT NULL DEFAULT 0,
    kills        INTEGER NOT NULL DEFAULT 0,
    deaths       INTEGER NOT NULL DEFAULT 0,
    assists      INTEGER NOT NULL DEFAULT 0,
    hs           INTEGER NOT NULL DEFAULT 0,
    bs           INTEGER NOT NULL DEFAULT 0,
    ls           INTEGER NOT NULL DEFAULT 0,
    kast         INTEGER NOT NULL DEFAULT 0,
    dmg_dealt    INTEGER NOT NULL DEFAULT 0,
    dmg_received INTEGER NOT NULL DEFAULT 0,
    won          INTEGER NOT NULL DEFAULT 0,
    agent        TEXT,
    team         TEXT,
    party        TEXT,
    PRIMARY KEY (match_id, puuid)
);
CREATE INDEX IF NOT EXISTS match_perf_by_player ON match_perf (puuid);
CREATE TABLE IF NOT EXISTS encounter_matches (
    match_id TEXT NOT NULL,
    puuid    TEXT NOT NULL,
    ts       TEXT,
    PRIMARY KEY (match_id, puuid)
);
CREATE INDEX IF NOT EXISTS encounter_matches_by_player ON encounter_matches (puuid);
CREATE TABLE IF NOT EXISTS match_labels (
    num      INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id TEXT NOT NULL UNIQUE,
    ts       TEXT
);
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS parsed_matches (
    match_id TEXT PRIMARY KEY
);
CREATE TABLE IF NOT EXISTS matches (
    match_id   TEXT PRIMARY KEY,
    map_id     TEXT,
    queue      TEXT,
    started_at TEXT,
    length     INTEGER NOT NULL DEFAULT 0,
    score      TEXT
);
CREATE TABLE IF NOT EXISTS shared_matches (
    match_id TEXT PRIMARY KEY,
    ts       TEXT
);
-- The downloaded pool. Kept apart from match_perf on purpose: these rows are
-- keyed by hash, not by puuid, they came from strangers rather than from this
-- machine's own downloads, and every query that leans on them says so. Merging
-- them into the local tables would make "what do I actually know" unanswerable.
CREATE TABLE IF NOT EXISTS pool_matches (
    mhash      TEXT PRIMARY KEY,
    map_id     TEXT,
    queue      TEXT,
    started_at TEXT,
    length     INTEGER NOT NULL DEFAULT 0,
    score      TEXT
);
CREATE TABLE IF NOT EXISTS pool_perf (
    mhash        TEXT NOT NULL,
    phash        TEXT NOT NULL,
    team         TEXT,
    party        TEXT,
    agent        TEXT,
    score        INTEGER NOT NULL DEFAULT 0,
    rounds       INTEGER NOT NULL DEFAULT 0,
    kills        INTEGER NOT NULL DEFAULT 0,
    deaths       INTEGER NOT NULL DEFAULT 0,
    assists      INTEGER NOT NULL DEFAULT 0,
    hs           INTEGER NOT NULL DEFAULT 0,
    bs           INTEGER NOT NULL DEFAULT 0,
    ls           INTEGER NOT NULL DEFAULT 0,
    kast         INTEGER NOT NULL DEFAULT 0,
    dmg_dealt    INTEGER NOT NULL DEFAULT 0,
    dmg_received INTEGER NOT NULL DEFAULT 0,
    won          INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (mhash, phash)
);
CREATE INDEX IF NOT EXISTS pool_perf_by_player ON pool_perf (phash);
-- What the form numbers said about a player the last time we met them. Written
-- once per encounter, read on the next one: this is the whole of how "they were
-- Gold 2 and rated 420 in June" survives to be compared with today.
CREATE TABLE IF NOT EXISTS form_snapshots (
    puuid   TEXT NOT NULL,
    ts      TEXT NOT NULL,
    rating  INTEGER NOT NULL DEFAULT 0,
    acs     REAL,
    kd      REAL,
    kast    REAL,
    hs      REAL,
    dd      REAL,
    matches INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (puuid, ts)
);
CREATE TABLE IF NOT EXISTS rank_snapshots (
    puuid    TEXT NOT NULL,
    match_id TEXT NOT NULL,
    ts       TEXT,
    tier     INTEGER NOT NULL DEFAULT 0,
    rr       INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (puuid, match_id)
);
CREATE TABLE IF NOT EXISTS ranked_matches (
    match_id TEXT PRIMARY KEY
);
CREATE TABLE IF NOT EXISTS calibration (
    id      INTEGER PRIMARY KEY CHECK (id = 1),
    players INTEGER NOT NULL DEFAULT 0,
    ts      TEXT,
    data    TEXT
);
CREATE TABLE IF NOT EXISTS pending_summaries (
    match_id  TEXT PRIMARY KEY,
    own_team  TEXT,
    queued_at TEXT,
    tries     INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS name_sources (
    puuid  TEXT NOT NULL,
    source TEXT NOT NULL,
    name   TEXT NOT NULL,
    ts     TEXT,
    PRIMARY KEY (puuid, source)
);
-- How a player behaved in one match, straight out of the match record. Kept
-- apart from match_perf because it answers a different question - whether the
-- game was worth playing, rather than how well it was played - and because
-- only the deep sweep fills it, so half of match_perf has no row here.
CREATE TABLE IF NOT EXISTS match_conduct (
    match_id         TEXT NOT NULL,
    puuid            TEXT NOT NULL,
    afk_rounds       INTEGER NOT NULL DEFAULT 0,
    spawn_rounds     INTEGER NOT NULL DEFAULT 0,
    penalised_rounds INTEGER NOT NULL DEFAULT 0,
    ff_damage        INTEGER NOT NULL DEFAULT 0,
    ff_taken         INTEGER NOT NULL DEFAULT 0,
    self_damage      INTEGER NOT NULL DEFAULT 0,
    session_minutes  INTEGER NOT NULL DEFAULT 0,
    account_level    INTEGER NOT NULL DEFAULT 0,
    party_size       INTEGER NOT NULL DEFAULT 0,
    party_penalty    INTEGER NOT NULL DEFAULT 0,
    rounds           INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (match_id, puuid)
);
CREATE INDEX IF NOT EXISTS match_conduct_by_player ON match_conduct (puuid);
-- What one ranked match paid, per player. The RR a match is worth is fixed the
-- moment it ends, so these rows are cached forever like every other match fact
-- here. mmr.py reads them; nothing else does.
CREATE TABLE IF NOT EXISTS rr_updates (
    puuid            TEXT NOT NULL,
    match_id         TEXT NOT NULL,
    ts               TEXT,
    tier_before      INTEGER NOT NULL DEFAULT 0,
    tier_after       INTEGER NOT NULL DEFAULT 0,
    rr_before        INTEGER NOT NULL DEFAULT 0,
    rr_after         INTEGER NOT NULL DEFAULT 0,
    earned           INTEGER NOT NULL DEFAULT 0,
    bonus            INTEGER NOT NULL DEFAULT 0,
    afk_penalty      INTEGER NOT NULL DEFAULT 0,
    rr_penalty       INTEGER NOT NULL DEFAULT 0,
    refund           INTEGER NOT NULL DEFAULT 0,
    map_bonus        INTEGER NOT NULL DEFAULT 0,
    placement        INTEGER NOT NULL DEFAULT 0,
    derank_protected INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (puuid, match_id)
);
CREATE INDEX IF NOT EXISTS rr_updates_by_player ON rr_updates (puuid);
"""

PERF_FIELDS = (
    "score",
    "rounds",
    "kills",
    "deaths",
    "assists",
    "hs",
    "bs",
    "ls",
    "kast",
    "dmg_dealt",
    "dmg_received",
    "won",
    "agent",
    "team",
    "party",
)

# Columns that arrived after the first release. Bolting them onto an existing
# cache is worth a little care: rebuilding it costs dozens of requests.
ADDED_COLUMNS = (
    ("match_perf", "agent", "TEXT"),
    ("match_perf", "team", "TEXT"),
    ("match_perf", "party", "TEXT"),
    ("encounters", "first_seen", "TEXT"),
    ("matches", "length", "INTEGER NOT NULL DEFAULT 0"),
    ("matches", "score", "TEXT"),
)


# Text columns want an empty string where the numbers want a zero.
BLANK_FIELD = {"agent": "", "team": "", "party": ""}


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _whole(value):
    """Whatever Riot put in a numeric field, as an int. Booleans count as 1."""
    try:
        return int(round(float(value or 0)))
    except (TypeError, ValueError):
        return 0


def _hundredths(value):
    """A fraction stored as whole hundredths, so its column can stay integer."""
    try:
        return int(round(float(value or 0) * 100))
    except (TypeError, ValueError):
        return 0


def _millis(value):
    """Riot's epoch milliseconds as the ISO text every other time here uses."""
    if not value:
        return None
    try:
        return datetime.fromtimestamp(float(value) / 1000, timezone.utc).isoformat(
            timespec="seconds"
        )
    except (OverflowError, OSError, TypeError, ValueError):
        return None


class Encounters:
    def __init__(self, enabled=True, path=None):
        # Resolved here rather than in the signature so that pointing DB_PATH
        # somewhere else - a test, a dry run - cannot still open the real file.
        self.enabled = enabled
        self.conn = None
        # How many batch() blocks deep we are. Above zero, the store methods
        # stop committing and leave it to the outermost block - see batch().
        self._batched = 0
        if enabled:
            self.conn = sqlite3.connect(path or DB_PATH)
            self._tune()
            self._migrate()
            self.conn.executescript(SCHEMA)
            self.conn.commit()

    def _tune(self):
        """Stop every commit costing a disk seek.

        The default journal is a rollback file and the default synchronous is
        FULL, which together mean each commit waits for two fsyncs. On a hard
        disk that is around a tenth of a second, and the deep sweep commits
        four times per match - so a three hundred match sweep spent two
        minutes of its life waiting for the platter rather than for Riot.

        WAL with synchronous=NORMAL costs one append and no wait. What is
        given up is narrow and worth naming: a power cut or a kernel panic in
        the wrong millisecond can lose the last transactions. Every one of
        them is a match Riot will hand over again for free, so the trade is
        several minutes per sweep against re-downloading a match that a crash
        interrupted. WAL also lets a reader run while a write is in flight,
        which is what makes the sweep's writer thread invisible to everything
        else in the process.
        """
        try:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA synchronous=NORMAL")
        except sqlite3.DatabaseError:
            # A database on a filesystem that will not do WAL - a network
            # share is the usual one - keeps the old settings and the old
            # speed. Nothing here needs WAL to be correct.
            pass

    @contextmanager
    def batch(self):
        """Hold the commits until the block ends, then make one.

        The store methods each commit, which is right when something calls one
        of them on its own and wrong when a caller is about to call four in a
        row about the same match. Nesting is allowed and only the outermost
        block commits.

        On the way out the commit happens whether or not the block raised: a
        half-written match is not a corrupt one here - every table is keyed by
        match id and written with INSERT OR REPLACE, so the next sweep to see
        that match fills in whatever is missing.
        """
        self._batched += 1
        try:
            yield self
        finally:
            self._batched -= 1
            if not self._batched and self.conn:
                self.conn.commit()

    def _commit(self):
        """A commit, unless a batch() block has taken charge of them."""
        if self.conn and not self._batched:
            self.conn.commit()

    def _migrate(self):
        """An older cache is missing columns; refetching is cheaper than guessing."""
        try:
            self._add_columns()
        except sqlite3.DatabaseError:
            return
        # Anything still missing after that is a column we cannot backfill, so
        # the cached breakdowns have to go and be pulled again.
        columns = {row[1] for row in self.conn.execute("PRAGMA table_info(match_perf)")}
        if columns and not set(PERF_FIELDS) <= columns:
            self.conn.executescript(
                "DROP TABLE IF EXISTS match_perf; DROP TABLE IF EXISTS parsed_matches;"
            )
        self.conn.commit()

    def _add_columns(self):
        """Widen existing tables in place - an empty column beats a lost cache.

        Rows written before the column existed simply read back as NULL, which
        every caller here already treats as "not known".
        """
        for table, column, kind in ADDED_COLUMNS:
            present = {row[1] for row in self.conn.execute(f"PRAGMA table_info({table})")}
            if present and column not in present:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")

    def cached_counts(self, puuids):
        """puuid -> how many of their match lines are already on disk.

        Not the same question as counts() above, which is how many times you
        have *met* them. This is how much of their history is already here,
        which is what says whether sweeping them will cost anything.
        """
        ids = [p for p in dict.fromkeys(puuids or ()) if p]
        if not self.conn or not ids:
            return {}
        marks = ",".join("?" * len(ids))
        cur = self.conn.execute(
            f"SELECT puuid, COUNT(*) FROM match_perf WHERE puuid IN ({marks}) GROUP BY puuid",
            ids,
        )
        return dict(cur.fetchall())

    def counts(self, puuids):
        """puuid -> how many previous matches we have logged them in."""
        if not self.conn or not puuids:
            return {}
        marks = ",".join("?" * len(puuids))
        cur = self.conn.execute(
            f"SELECT puuid, times FROM encounters WHERE puuid IN ({marks})", list(puuids)
        )
        return dict(cur.fetchall())

    def last_ranks(self, puuids):
        """puuid -> the most recent rank we ever saw them at, and when.

        What this is for: someone who plays behind Incognito has no rank in the
        live lobby, because the one place it could come from is a lookup we do
        not make about them. But we have met them before, and the record of
        that match said what they were. It is not their rank today - it is the
        last one we know of, which is why it is shown as such.
        """
        if not self.conn or not puuids:
            return {}
        marks = ",".join("?" * len(puuids))
        cur = self.conn.execute(
            f"""
            SELECT puuid, tier, rr, ts FROM rank_snapshots
            WHERE puuid IN ({marks}) AND tier > 0
            ORDER BY ts DESC, rr DESC
            """,
            list(puuids),
        )
        out = {}
        for puuid, tier, rr, ts in cur.fetchall():
            # Newest first, and on a tie the one that carries RR: that is the
            # snapshot taken from a live lobby rather than from a match record,
            # and it knows strictly more.
            out.setdefault(puuid, {"tier": tier, "rr": rr, "ts": ts})
        return out

    def ranks_read(self, match_ids):
        """Of these matches, the ones whose ranks have already been read.

        A match is marked the moment it is read, whether or not it had a rank
        in it for anybody - so this answers "we looked and there was nothing
        for this player" as well as "we looked and wrote it down". That is the
        difference between looking a hidden player up once and looking them up
        every lobby for ever.
        """
        if not self.conn or not match_ids:
            return set()
        marks = ",".join("?" * len(match_ids))
        cur = self.conn.execute(
            f"SELECT match_id FROM ranked_matches WHERE match_id IN ({marks})",
            list(match_ids),
        )
        return {row[0] for row in cur.fetchall()}

    def store_rank_snapshots(self, match_id, tiers):
        """Record what rank each player was, from the record of a match.

        Only fills gaps: a snapshot already taken from the live lobby carries
        RR as well and is the better one, so it is left alone.
        """
        if not self.conn or not match_id:
            return 0
        # Marked whether or not it had anything to give: a match where nobody
        # was ranked has still been read, and must not be re-read for ever.
        self.conn.execute(
            "INSERT OR IGNORE INTO ranked_matches (match_id) VALUES (?)", (match_id,)
        )
        now = _now()
        stored = 0
        for puuid, tier in (tiers or {}).items():
            if not puuid or not tier:
                continue
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO rank_snapshots (puuid, match_id, ts, tier, rr) "
                "VALUES (?, ?, ?, ?, 0)",
                (puuid, match_id, now, int(tier)),
            )
            stored += cur.rowcount or 0
        self._commit()
        return stored

    def nameless(self, limit=200):
        """Players we have met but never managed to put a name to.

        Everyone here was behind Incognito every time we saw them. The name
        service will answer about them now that those matches are over.
        """
        if not self.conn:
            return []
        cur = self.conn.execute(
            "SELECT puuid FROM encounters WHERE COALESCE(name, '') = '' "
            "ORDER BY last_seen DESC LIMIT ?",
            (int(limit),),
        )
        return [row[0] for row in cur.fetchall()]

    def record(self, match_id, rows):
        """Count a match once, no matter how often we re-render it."""
        if not self.conn or not match_id:
            return
        cur = self.conn.execute("SELECT 1 FROM seen_matches WHERE match_id = ?", (match_id,))
        if cur.fetchone():
            return
        now = _now()
        self.conn.execute("INSERT INTO seen_matches (match_id, ts) VALUES (?, ?)", (match_id, now))
        self._label(match_id, now)
        for row in rows:
            if not row.puuid or row.is_self:
                continue
            # Which match, not just how many - so `who` can name the games.
            self.conn.execute(
                "INSERT OR IGNORE INTO encounter_matches (match_id, puuid, ts) VALUES (?, ?, ?)",
                (match_id, row.puuid, now),
            )
            self.conn.execute(
                """
                INSERT INTO encounters (puuid, name, times, last_seen, first_seen)
                VALUES (?, ?, 1, ?, ?)
                ON CONFLICT(puuid) DO UPDATE SET
                    times = times + 1,
                    name = COALESCE(NULLIF(excluded.name, ''), encounters.name),
                    last_seen = excluded.last_seen,
                    first_seen = COALESCE(encounters.first_seen, excluded.first_seen)
                """,
                (row.puuid, row.name or "", now, now),
            )
            # Their rank at the time we met - free, we already asked for it.
            if row.tier:
                self.conn.execute(
                    "INSERT OR REPLACE INTO rank_snapshots (puuid, match_id, ts, tier, rr) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (row.puuid, match_id, now, row.tier, row.rr or 0),
                )
        self.conn.commit()

    def known_names(self, puuids):
        """[(puuid, name, last_seen)] for the ones we can already name.

        The cheapest way there is of naming somebody hiding behind Incognito:
        we met them before, when they were not, and wrote it down. No request,
        no network, and it answers while the match is still being played.
        `last_seen` dates the claim - it is who they were, not who they are.
        """
        unique = [p for p in dict.fromkeys(puuids or ()) if p]
        if not self.conn or not unique:
            return []
        marks = ",".join("?" * len(unique))
        cur = self.conn.execute(
            f"SELECT puuid, name, last_seen FROM encounters WHERE puuid IN ({marks}) "
            "AND COALESCE(name, '') != ''",
            unique,
        )
        return cur.fetchall()

    def remember_names(self, names, source="name-service"):
        """Fill in Riot IDs for people already logged as an encounter.

        Somebody who played behind Incognito was logged without a name; one of
        the sources in identity.py has it. Only rows that already exist are
        touched - this renames a player we have met, it does not invent one.

        Where the name came from is written down next to it whatever happens,
        including for a name we already had. Two sources agreeing is worth
        knowing, and so is `who` being able to say that a Riot ID came off a
        leaderboard dump from Tuesday rather than out of the name service.
        """
        if not self.conn or not names:
            return 0
        now = _now()
        updated = 0
        for puuid, name in names.items():
            if not puuid or not name:
                continue
            self.conn.execute(
                "INSERT INTO name_sources (puuid, source, name, ts) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(puuid, source) DO UPDATE SET name = excluded.name, ts = excluded.ts",
                (puuid, source or "?", name, now),
            )
            cur = self.conn.execute(
                "UPDATE encounters SET name = ? WHERE puuid = ? AND COALESCE(name, '') != ?",
                (name, puuid, name),
            )
            updated += max(0, cur.rowcount)
        self.conn.commit()
        return updated

    def remember_found(self, found):
        """The same, for a {puuid: identity.Named} straight out of a Resolver."""
        renamed = 0
        by_source = {}
        for puuid, named in (found or {}).items():
            by_source.setdefault(named.source, {})[puuid] = named.name
        for source, names in by_source.items():
            renamed += self.remember_names(names, source=source)
        return renamed

    def name_provenance(self, puuid):
        """[(source, name, ts)] - every source that has ever named one player."""
        if not self.conn or not puuid:
            return []
        cur = self.conn.execute(
            "SELECT source, name, ts FROM name_sources WHERE puuid = ? ORDER BY ts DESC",
            (puuid,),
        )
        return cur.fetchall()

    def teammates(self, puuid, limit=5):
        """[(puuid, name, shared)] - who this player keeps turning up beside.

        Not a name and never claimed to be one: it is the lead you follow when
        nothing can name a hidden player directly. Somebody who queues with
        the same visible person every evening is findable through them, and
        this says who to look at. Same-side matches only - meeting as
        opponents is just matchmaking.
        """
        if not self.conn or not puuid:
            return []
        cur = self.conn.execute(
            "SELECT b.puuid, COALESCE(e.name, ''), COUNT(*) AS shared FROM match_perf a "
            "JOIN match_perf b ON b.match_id = a.match_id AND b.team = a.team "
            "AND b.puuid != a.puuid "
            "LEFT JOIN encounters e ON e.puuid = b.puuid "
            "WHERE a.puuid = ? AND COALESCE(a.team, '') != '' "
            "GROUP BY b.puuid ORDER BY shared DESC, b.puuid LIMIT ?",
            (puuid, int(limit)),
        )
        return cur.fetchall()

    # ------------------------------------------------- which match, and when

    def _label(self, match_id, ts=None):
        """Hand a match a short number of its own, and keep it forever.

        Riot's match id is a 36-character uuid - fine for a lookup, useless in a
        table you read with your eyes. The number here is local to this install:
        M12 means nothing to anyone else, but `match M12` gives back the uuid.
        """
        if not self.conn or not match_id:
            return None
        self.conn.execute(
            "INSERT OR IGNORE INTO match_labels (match_id, ts) VALUES (?, ?)", (match_id, ts)
        )
        cur = self.conn.execute("SELECT num FROM match_labels WHERE match_id = ?", (match_id,))
        row = cur.fetchone()
        return row[0] if row else None

    def label_missing(self):
        """Number every logged match that has not been numbered yet.

        Oldest first, so the numbers run roughly in the order you played them.
        """
        if not self.conn:
            return 0
        cur = self.conn.execute(
            """
            SELECT e.match_id, MIN(COALESCE(e.ts, m.started_at))
            FROM encounter_matches e
            LEFT JOIN matches m ON m.match_id = e.match_id
            WHERE e.match_id NOT IN (SELECT match_id FROM match_labels)
            GROUP BY e.match_id
            ORDER BY 2
            """
        )
        added = 0
        for match_id, ts in cur.fetchall():
            self.conn.execute(
                "INSERT OR IGNORE INTO match_labels (match_id, ts) VALUES (?, ?)", (match_id, ts)
            )
            added += 1
        self.conn.commit()
        return added

    def link_own_matches(self, self_puuid):
        """Recover encounters from matches of your own that are already cached.

        A cached match holds the line of all ten players, so every match with a
        line of yours in it is a lobby you shared with nine other people - even
        the ones played long before this table existed. Matches downloaded for
        somebody else's form have no line of yours and are skipped.
        """
        if not self.conn or not self_puuid:
            return 0
        before = self.conn.execute("SELECT COUNT(*) FROM encounter_matches").fetchone()[0]
        self.conn.execute(
            """
            INSERT OR IGNORE INTO encounter_matches (match_id, puuid, ts)
            SELECT them.match_id, them.puuid, COALESCE(m.started_at, s.ts)
            FROM match_perf them
            JOIN match_perf mine
              ON mine.match_id = them.match_id AND mine.puuid = ?
            LEFT JOIN matches m ON m.match_id = them.match_id
            LEFT JOIN seen_matches s ON s.match_id = them.match_id
            WHERE them.puuid <> ?
            """,
            (self_puuid, self_puuid),
        )
        self.conn.commit()
        added = self.conn.execute("SELECT COUNT(*) FROM encounter_matches").fetchone()[0] - before
        self.label_missing()
        return added

    def met_in(self, puuid, limit=None):
        """The matches you have shared with one player, newest first."""
        if not self.conn or not puuid:
            return []
        cur = self.conn.execute(
            """
            SELECT e.match_id, l.num, COALESCE(e.ts, m.started_at), m.map_id, m.queue
            FROM encounter_matches e
            LEFT JOIN match_labels l ON l.match_id = e.match_id
            LEFT JOIN matches m ON m.match_id = e.match_id
            WHERE e.puuid = ?
            ORDER BY COALESCE(e.ts, m.started_at, '') DESC, l.num DESC
            """
            + ("LIMIT ?" if limit else ""),
            (puuid, limit) if limit else (puuid,),
        )
        fields = ("match_id", "num", "ts", "map_id", "queue")
        return [dict(zip(fields, row)) for row in cur.fetchall()]

    def match_by_label(self, num):
        """One numbered match: the uuid behind it, and what we know about it."""
        if not self.conn or not num:
            return None
        cur = self.conn.execute(
            """
            SELECT l.num, l.match_id, COALESCE(m.started_at, l.ts, s.ts), m.map_id, m.queue
            FROM match_labels l
            LEFT JOIN matches m ON m.match_id = l.match_id
            LEFT JOIN seen_matches s ON s.match_id = l.match_id
            WHERE l.num = ?
            """,
            (num,),
        )
        row = cur.fetchone()
        if not row:
            return None
        return dict(zip(("num", "match_id", "ts", "map_id", "queue"), row))

    def latest_match(self):
        """The most recent match this program watched, in match_by_label's shape.

        What the deep report falls back on when the game is closed: the last
        lobby it saw is almost always the one being asked about.
        """
        if not self.conn:
            return None
        row = self.conn.execute(
            "SELECT num FROM match_labels ORDER BY num DESC LIMIT 1"
        ).fetchone()
        return self.match_by_label(row[0]) if row else None

    def roster(self, match_id):
        """Everyone we have a name for in one match, most-met first."""
        if not self.conn or not match_id:
            return []
        cur = self.conn.execute(
            """
            SELECT e.puuid, c.name, c.times
            FROM encounter_matches e
            LEFT JOIN encounters c ON c.puuid = e.puuid
            WHERE e.match_id = ?
            ORDER BY COALESCE(c.times, 0) DESC, c.name
            """,
            (match_id,),
        )
        return [dict(zip(("puuid", "name", "times"), row)) for row in cur.fetchall()]

    # ---------------------------------------------------------------- settings

    def setting(self, key, default=None):
        if not self.conn:
            return default
        cur = self.conn.execute("SELECT value FROM settings WHERE key = ?", (key,))
        row = cur.fetchone()
        return row[0] if row and row[0] else default

    def remember(self, key, value):
        """Small facts worth keeping between runs - your own puuid, so far."""
        if not self.conn or not value:
            return
        self.conn.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, str(value))
        )
        self.conn.commit()

    def remember_own(self, puuid):
        """Write down an account of our own, without forgetting the last one.

        `self_puuid` was one key holding one puuid, and one puuid is the wrong
        shape for this: a second account on the same machine overwrote the
        first, and every cached match played on the other one then had no "us"
        in it at all - which is how a full report on a perfectly good lobby
        came to say it could not tell the two sides apart. So the newest is
        still `self_puuid`, because that is the one everything else reads, and
        all of them are kept beside it.
        """
        if not self.conn or not puuid:
            return
        known = self.own_puuids()
        self.remember("self_puuid", puuid)
        if puuid not in known:
            self.remember("self_puuids", " ".join([puuid, *known]))

    def own_puuids(self):
        """Every account this machine has played on, newest first.

        Three sources, and the third is the one that makes this work on a
        database that predates it. A match this program watched put its other
        nine players in encounter_matches and never itself, so the player in a
        watched match who was never an encounter *is* us - which recovers an
        account that was never written down, including one replaced by the
        next login before any of this existed.
        """
        if not self.conn:
            return []
        out = []
        for puuid in [self.setting("self_puuid") or ""] + (
            self.setting("self_puuids") or ""
        ).split():
            if puuid and puuid not in out:
                out.append(puuid)
        for (puuid,) in self.conn.execute(
            """
            SELECT p.puuid FROM match_perf p
            WHERE p.match_id IN (SELECT match_id FROM encounter_matches)
              AND NOT EXISTS (
                  SELECT 1 FROM encounter_matches e
                  WHERE e.match_id = p.match_id AND e.puuid = p.puuid
              )
            GROUP BY p.puuid ORDER BY COUNT(*) DESC
            """
        ).fetchall():
            if puuid and puuid not in out:
                out.append(puuid)
        return out

    # ------------------------------------------------------- match breakdowns

    def is_parsed(self, match_id):
        """Have we already torn this match apart? Results never change."""
        if not self.conn:
            return False
        cur = self.conn.execute("SELECT 1 FROM parsed_matches WHERE match_id = ?", (match_id,))
        return cur.fetchone() is not None

    def store_match_perf(self, match_id, per_player):
        """per_player: puuid -> dict of PERF_FIELDS, for every player in the match."""
        if not self.conn or not match_id:
            return
        columns = ", ".join(PERF_FIELDS)
        marks = ", ".join("?" * len(PERF_FIELDS))
        self.conn.executemany(
            f"INSERT OR REPLACE INTO match_perf (match_id, puuid, {columns}) "
            f"VALUES (?, ?, {marks})",
            [
                (match_id, puuid, *[entry.get(f, BLANK_FIELD.get(f, 0)) for f in PERF_FIELDS])
                for puuid, entry in per_player.items()
                if puuid
            ],
        )
        self.conn.execute("INSERT OR IGNORE INTO parsed_matches (match_id) VALUES (?)", (match_id,))
        self._commit()

    def store_match_meta(self, match_id, meta):
        """Map, queue and start time of a match we already downloaded.

        Free: it rides along with the details payload the breakdown came from,
        and it is what lets the pick advice know which map you are loading into.
        """
        if not self.conn or not match_id or not meta:
            return
        self.conn.execute(
            "INSERT OR REPLACE INTO matches "
            "(match_id, map_id, queue, started_at, length, score) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                match_id,
                meta.get("map_id") or "",
                meta.get("queue") or "",
                meta.get("started_at"),
                int(meta.get("length") or 0),
                meta.get("score") or "",
            ),
        )
        self._commit()

    # ------------------------------------------------ matches still unpublished

    def queue_summary(self, match_id, own_team=""):
        """Remember a finished match Riot has not published yet.

        The record of a match lands a moment after the game hands you back to
        the menus - usually. When it takes longer than anyone is willing to sit
        and watch, the match used to be dropped and the scoreboard lost for
        good. Writing it down instead costs one row and makes the wait outlive
        both the next lobby and the program itself.
        """
        if not self.conn or not match_id:
            return
        self.conn.execute(
            "INSERT OR IGNORE INTO pending_summaries (match_id, own_team, queued_at, tries) "
            "VALUES (?, ?, ?, 0)",
            (match_id, own_team or "", datetime.now(timezone.utc).isoformat(timespec="seconds")),
        )
        self.conn.commit()

    def pending_summaries(self):
        """[{match_id, own_team, queued_at, tries}] - oldest first."""
        if not self.conn:
            return []
        cur = self.conn.execute(
            "SELECT match_id, own_team, queued_at, tries FROM pending_summaries "
            "ORDER BY COALESCE(queued_at, '')"
        )
        return [
            dict(zip(("match_id", "own_team", "queued_at", "tries"), row))
            for row in cur.fetchall()
        ]

    def note_summary_try(self, match_id):
        """Count one more attempt against a queued match."""
        if not self.conn or not match_id:
            return
        self.conn.execute(
            "UPDATE pending_summaries SET tries = tries + 1 WHERE match_id = ?", (match_id,)
        )
        self.conn.commit()

    def drop_summary(self, match_id):
        """Forget a queued match - it arrived, or it never will."""
        if not self.conn or not match_id:
            return
        self.conn.execute("DELETE FROM pending_summaries WHERE match_id = ?", (match_id,))
        self.conn.commit()

    def perf_rows(self, puuid, match_ids):
        """Cached per-match lines for one player, in whatever order SQLite gives."""
        if not self.conn or not match_ids:
            return []
        marks = ",".join("?" * len(match_ids))
        columns = ", ".join(PERF_FIELDS)
        cur = self.conn.execute(
            f"SELECT {columns} FROM match_perf WHERE puuid = ? AND match_id IN ({marks})",
            [puuid, *match_ids],
        )
        return [dict(zip(PERF_FIELDS, row)) for row in cur.fetchall()]

    def recent_lines(self, puuid, limit=30, queue=None):
        """One player's cached match lines, newest first, with the match meta.

        The backbone of the deep profile: everything it reports - the agents,
        the sessions, the KDA, which queues those came from - is this list read
        a different way. Lines whose match was never given a start time sort
        last rather than dropping out, because a line with no date still counts
        towards an agent pool.
        """
        if not self.conn or not puuid:
            return []
        columns = ", ".join(f"p.{field}" for field in PERF_FIELDS)
        sql = (
            # m.score is the match scoreline and p.score the player's combat
            # score; the match one is renamed so the two cannot collide in the
            # dict the row becomes.
            f"SELECT {columns}, p.match_id, m.map_id, m.queue, m.started_at, m.length, "
            "m.score AS match_score "
            "FROM match_perf p LEFT JOIN matches m ON m.match_id = p.match_id "
            "WHERE p.puuid = ? "
        )
        params = [puuid]
        if queue:
            sql += "AND m.queue = ? "
            params.append(queue)
        sql += "ORDER BY COALESCE(m.started_at, '') DESC LIMIT ?"
        params.append(int(limit))
        fields = (
            *PERF_FIELDS,
            "match_id",
            "map_id",
            "queue",
            "started_at",
            "length",
            "match_score",
        )
        return [dict(zip(fields, row)) for row in self.conn.execute(sql, params)]

    def all_perf_rows(self, puuid):
        """Every cached line for one player, with the map each was played on.

        The map comes from a left join, so lines cached before maps were
        recorded still come back - just with nothing in that field.
        """
        if not self.conn:
            return []
        columns = ", ".join(f"p.{field}" for field in PERF_FIELDS)
        cur = self.conn.execute(
            f"SELECT {columns}, m.map_id FROM match_perf p "
            "LEFT JOIN matches m ON m.match_id = p.match_id WHERE p.puuid = ?",
            (puuid,),
        )
        fields = (*PERF_FIELDS, "map_id")
        return [dict(zip(fields, row)) for row in cur.fetchall()]

    def matches_missing_conduct(self, match_ids):
        """Which of these matches have no behaviour rows cached yet.

        A match parsed before match_conduct existed is in match_perf and has
        nothing here, and the difference cannot be recovered from what was
        stored - only from the payload, which means another request. This is
        what tells the deep sweep which ones are worth one.
        """
        ids = [m for m in dict.fromkeys(match_ids or ()) if m]
        if not self.conn or not ids:
            return set()
        marks = ",".join("?" * len(ids))
        cur = self.conn.execute(
            f"SELECT DISTINCT match_id FROM match_conduct WHERE match_id IN ({marks})",
            ids,
        )
        return set(ids) - {row[0] for row in cur.fetchall()}

    def matches_missing_team(self):
        """Cached matches parsed before sides were recorded, for a re-read.

        Their lines still score fine - the team only matters to party
        detection, which simply skips them until backfill fetches them again.
        """
        if not self.conn:
            return set()
        cur = self.conn.execute(
            "SELECT DISTINCT match_id FROM match_perf WHERE team IS NULL OR team = ''"
        )
        return {row[0] for row in cur.fetchall()}

    def matches_missing_ranks(self):
        """Cached matches parsed before ranks were read out of the record.

        Same idea as matches_missing_team: their numbers are fine, they just
        never gave up the one field that lets a hidden player be ranked later.
        Re-reading is one request each and only ever happens once, so it rides
        along with a backfill that was asked for anyway.
        """
        if not self.conn:
            return set()
        cur = self.conn.execute(
            "SELECT DISTINCT match_id FROM match_perf WHERE match_id NOT IN "
            "(SELECT match_id FROM ranked_matches)"
        )
        return {row[0] for row in cur.fetchall()}

    def matches_missing_party(self):
        """Cached matches parsed before Riot's own party ids were read out.

        Same shape as matches_missing_team: the numbers in them are fine, they
        are simply missing the one field that turns party detection from a
        guess into a fact. Re-reading is one request each and happens once.
        """
        if not self.conn:
            return set()
        cur = self.conn.execute(
            "SELECT DISTINCT match_id FROM match_perf WHERE party IS NULL OR party = ''"
        )
        return {row[0] for row in cur.fetchall()}

    # --------------------------------------------------------------- sharing

    def unshared(self, limit=50):
        """Match ids that are fully parsed and have never been uploaded.

        A match is only offered when it is whole: a start time, and ten lines
        that all know which side they were on. Half a scoreboard is the one
        thing a shared dataset cannot recover from, and a match cached before
        sides were recorded is not sent at all rather than sent blind - it
        stays here until a backfill fills the sides in, and goes then.

        Oldest first, so an interrupted upload resumes where it stopped rather
        than starting again from today.
        """
        if not self.conn:
            return []
        cur = self.conn.execute(
            """
            SELECT m.match_id FROM matches m
            JOIN parsed_matches p ON p.match_id = m.match_id
            JOIN match_perf f ON f.match_id = m.match_id
            WHERE m.started_at IS NOT NULL AND m.started_at != ''
              AND m.match_id NOT IN (SELECT match_id FROM shared_matches)
              AND f.team IS NOT NULL AND f.team != ''
            GROUP BY m.match_id
            HAVING COUNT(*) = 10
            ORDER BY m.started_at
            LIMIT ?
            """,
            (int(limit),),
        )
        return [row[0] for row in cur.fetchall()]

    def mark_shared(self, match_ids):
        """Write down that these went out, so they never go out twice."""
        if not self.conn or not match_ids:
            return 0
        now = _now()
        cur = self.conn.executemany(
            "INSERT OR IGNORE INTO shared_matches (match_id, ts) VALUES (?, ?)",
            [(match_id, now) for match_id in match_ids if match_id],
        )
        self.conn.commit()
        return cur.rowcount or 0

    def forget_shared(self):
        """Clear the upload ledger, so everything is offered again."""
        if not self.conn:
            return
        self.conn.execute("DELETE FROM shared_matches")
        self.conn.commit()

    def share_counts(self):
        """{shared, parsed, waiting} - what the share status line prints."""
        if not self.conn:
            return {"shared": 0, "parsed": 0, "waiting": 0}

        def one(sql):
            return self.conn.execute(sql).fetchone()[0] or 0

        shared = one("SELECT COUNT(*) FROM shared_matches")
        parsed = one(
            "SELECT COUNT(*) FROM matches m JOIN parsed_matches p "
            "ON p.match_id = m.match_id WHERE m.started_at IS NOT NULL AND m.started_at != ''"
        )
        return {"shared": shared, "parsed": parsed, "waiting": max(0, parsed - shared)}

    def bundle(self, match_ids):
        """Everything known about these matches, as {match_id: {meta, lines}}.

        This is the whole of what leaves the machine when sharing is on, and it
        is deliberately assembled in one place so that what is sent can be read
        off a single function. Note what is *not* selected: no name, no rank
        snapshot, no encounter count, nothing from the encounters table at all.
        """
        if not self.conn or not match_ids:
            return {}
        ids = [m for m in match_ids if m]
        marks = ",".join("?" * len(ids))
        out = {}
        for row in self.conn.execute(
            f"SELECT match_id, map_id, queue, started_at, length, score "
            f"FROM matches WHERE match_id IN ({marks})",
            ids,
        ):
            out[row[0]] = {
                "meta": dict(zip(("map_id", "queue", "started_at", "length", "score"), row[1:])),
                "lines": [],
            }
        columns = ", ".join(PERF_FIELDS)
        for row in self.conn.execute(
            f"SELECT match_id, puuid, {columns} FROM match_perf WHERE match_id IN ({marks})",
            ids,
        ):
            entry = out.get(row[0])
            if entry is not None:
                line = dict(zip(PERF_FIELDS, row[2:]))
                line["puuid"] = row[1]
                entry["lines"].append(line)
        return out

    # ------------------------------------------------------------ progress

    def rank_before(self, puuids, exclude=None):
        """puuid -> the last rank we saw them at *before* this match.

        last_ranks answers "what is the best rank we know"; this answers "what
        were they when we last met", which is a different question and the one
        progress is measured against. The match on screen is excluded, because
        a snapshot taken thirty seconds ago is not a previous meeting.
        """
        if not self.conn or not puuids:
            return {}
        marks = ",".join("?" * len(puuids))
        sql = (
            f"SELECT puuid, tier, rr, ts FROM rank_snapshots "
            f"WHERE puuid IN ({marks}) AND tier > 0 "
        )
        params = list(puuids)
        if exclude:
            sql += "AND match_id != ? "
            params.append(exclude)
        sql += "ORDER BY ts DESC, rr DESC"
        out = {}
        for puuid, tier, rr, ts in self.conn.execute(sql, params):
            out.setdefault(puuid, {"tier": tier, "rr": rr, "ts": ts})
        return out

    def store_form(self, puuid, summary):
        """Write down what the form numbers said about somebody today.

        One row per player per day: meeting the same person four times in an
        evening should not fill the table with four near-identical readings,
        and "what were they in March" wants a date, not a timestamp.
        """
        if not self.conn or not puuid or not summary:
            return
        self.conn.execute(
            "INSERT OR REPLACE INTO form_snapshots "
            "(puuid, ts, rating, acs, kd, kast, hs, dd, matches) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                puuid,
                _now()[:10],
                int(summary.get("rating") or 0),
                summary.get("acs"),
                summary.get("kd"),
                summary.get("kast"),
                summary.get("hs"),
                summary.get("dd"),
                int(summary.get("matches") or 0),
            ),
        )
        self.conn.commit()

    def form_before(self, puuids, before=None):
        """puuid -> the form reading from the last day we met them.

        Today's reading is skipped: comparing a number with itself is how a
        column full of "+0" gets built.
        """
        if not self.conn or not puuids:
            return {}
        marks = ",".join("?" * len(puuids))
        cutoff = before or _now()[:10]
        cur = self.conn.execute(
            f"SELECT puuid, ts, rating, acs, kd, kast, hs, dd, matches "
            f"FROM form_snapshots WHERE puuid IN ({marks}) AND ts < ? "
            "ORDER BY ts DESC",
            [*puuids, cutoff],
        )
        fields = ("ts", "rating", "acs", "kd", "kast", "hs", "dd", "matches")
        out = {}
        for row in cur.fetchall():
            out.setdefault(row[0], dict(zip(fields, row[1:])))
        return out

    def met_before(self, puuids, exclude=None):
        """puuid -> how many earlier matches we have logged them in."""
        if not self.conn or not puuids:
            return {}
        marks = ",".join("?" * len(puuids))
        sql = f"SELECT puuid, COUNT(*) FROM encounter_matches WHERE puuid IN ({marks}) "
        params = list(puuids)
        if exclude:
            sql += "AND match_id != ? "
            params.append(exclude)
        sql += "GROUP BY puuid"
        return dict(self.conn.execute(sql, params).fetchall())

    # ------------------------------------------------------------ the pool

    POOL_FIELDS = (
        "team", "party", "agent", "score", "rounds", "kills", "deaths", "assists",
        "hs", "bs", "ls", "kast", "dmg_dealt", "dmg_received", "won",
    )

    def replace_pool(self, matches):
        """Swap the downloaded pool for a freshly downloaded one, wholesale.

        Not merged: the pool file is the whole truth about the pool every time
        it is fetched, and merging would keep matches the pool has since thrown
        out. Done in one transaction, so a download that dies half way leaves
        the old pool intact rather than half of each.
        """
        if not self.conn:
            return 0
        rows = []
        meta = []
        for match in matches:
            mhash = match.get("m")
            if not mhash:
                continue
            meta.append((
                mhash,
                match.get("map") or "",
                match.get("q") or "",
                match.get("t") or "",
                int(match.get("len") or 0),
                match.get("sc") or "",
            ))
            for line in match.get("p") or ():
                if not line.get("p"):
                    continue
                rows.append((
                    mhash, line["p"], line.get("t") or "", line.get("g") or "",
                    line.get("c") or "",
                    int(line.get("sc") or 0), int(line.get("r") or 0),
                    int(line.get("k") or 0), int(line.get("d") or 0),
                    int(line.get("a") or 0), int(line.get("hs") or 0),
                    int(line.get("bs") or 0), int(line.get("ls") or 0),
                    int(line.get("kast") or 0), int(line.get("dd") or 0),
                    int(line.get("dr") or 0), 1 if line.get("w") else 0,
                ))
        columns = ", ".join(self.POOL_FIELDS)
        marks = ", ".join("?" * len(self.POOL_FIELDS))
        with self.conn:
            self.conn.execute("DELETE FROM pool_perf")
            self.conn.execute("DELETE FROM pool_matches")
            self.conn.executemany(
                "INSERT OR REPLACE INTO pool_matches "
                "(mhash, map_id, queue, started_at, length, score) VALUES (?, ?, ?, ?, ?, ?)",
                meta,
            )
            self.conn.executemany(
                f"INSERT OR REPLACE INTO pool_perf (mhash, phash, {columns}) "
                f"VALUES (?, ?, {marks})",
                rows,
            )
        return len(meta)

    def pool_counts(self):
        """{matches, players, lines, first, last} for what was downloaded."""
        blank = {"matches": 0, "players": 0, "lines": 0, "first": "", "last": ""}
        if not self.conn:
            return blank
        span = self.conn.execute(
            "SELECT MIN(started_at), MAX(started_at) FROM pool_matches"
        ).fetchone()
        def one(sql):
            return self.conn.execute(sql).fetchone()[0] or 0
        return {
            "matches": one("SELECT COUNT(*) FROM pool_matches"),
            "players": one("SELECT COUNT(DISTINCT phash) FROM pool_perf"),
            "lines": one("SELECT COUNT(*) FROM pool_perf"),
            "first": (span[0] or "")[:10],
            "last": (span[1] or "")[:10],
        }

    def _pool_pairs(self, hashes, join_on):
        """Shared rows in the pool for every pair among `hashes`, a < b.

        Callers hand over hashes because only they know the salt: this layer
        never sees a puuid from the pool and could not hash one if it wanted.
        """
        unique = sorted({h for h in (hashes or ()) if h})
        if not self.conn or len(unique) < 2:
            return {}
        marks = ",".join("?" * len(unique))
        sql = (
            "SELECT a.phash, b.phash, m.started_at FROM pool_perf a "
            f"JOIN pool_perf b ON b.mhash = a.mhash AND b.{join_on} = a.{join_on} "
            "AND b.phash > a.phash "
            "LEFT JOIN pool_matches m ON m.mhash = a.mhash "
            f"WHERE a.phash IN ({marks}) AND b.phash IN ({marks}) "
            f"AND a.{join_on} IS NOT NULL AND a.{join_on} != '' "
        )
        out = {}
        for a, b, started_at in self.conn.execute(sql, [*unique, *unique]):
            out.setdefault((a, b), []).append(started_at)
        return out

    def pool_timeline(self, hashes):
        """Pairs who played the same side in a pooled match - the inference."""
        return self._pool_pairs(hashes, "team")

    def pool_parties(self, hashes):
        """Pairs the pool says were in one party - not an inference at all.

        Riot records which party every player queued in, and a pooled match
        carries it, so this answers outright the question party.py otherwise
        has to guess at.
        """
        return self._pool_pairs(hashes, "party")

    def pool_samples(self, hashes):
        """hash -> how many pooled matches we know their side in."""
        unique = sorted({h for h in (hashes or ()) if h})
        if not self.conn or not unique:
            return {}
        marks = ",".join("?" * len(unique))
        cur = self.conn.execute(
            f"SELECT phash, COUNT(*) FROM pool_perf WHERE phash IN ({marks}) "
            "AND team IS NOT NULL AND team != '' GROUP BY phash",
            unique,
        )
        return dict(cur.fetchall())

    def pool_lines(self, phash, limit=40):
        """Pooled scoreboard lines for one hashed player, newest first."""
        if not self.conn or not phash:
            return []
        columns = ", ".join(f"p.{f}" for f in self.POOL_FIELDS)
        cur = self.conn.execute(
            f"SELECT {columns}, m.map_id FROM pool_perf p "
            "LEFT JOIN pool_matches m ON m.mhash = p.mhash "
            "WHERE p.phash = ? ORDER BY COALESCE(m.started_at, '') DESC LIMIT ?",
            (phash, int(limit)),
        )
        fields = (*self.POOL_FIELDS, "map_id")
        return [dict(zip(fields, row)) for row in cur.fetchall()]

    # ------------------------------------------------------------- parties

    def party_timeline(self, puuids, exclude=None):
        """{(a, b): [match times]} for every pair among `puuids`, a < b.

        One entry per match the two played on the *same* side - meeting as
        opponents says nothing about queueing together - holding the start time
        where the cache recorded one and None where it did not. Counting those
        entries gives what party_evidence used to return; spreading them out
        over the clock is what tells a party from a coincidence, and party.weigh
        is where that reading happens.

        Costs no requests: the matches were downloaded for the form numbers and
        are already here, start times and all.
        """
        unique = sorted({p for p in (puuids or ()) if p})
        if not self.conn or len(unique) < 2:
            return {}
        marks = ",".join("?" * len(unique))
        sql = (
            "SELECT a.puuid, b.puuid, m.started_at FROM match_perf a "
            "JOIN match_perf b ON b.match_id = a.match_id "
            "AND b.team = a.team AND b.puuid > a.puuid "
            "LEFT JOIN matches m ON m.match_id = a.match_id "
            f"WHERE a.puuid IN ({marks}) AND b.puuid IN ({marks}) "
            "AND a.team IS NOT NULL AND a.team != '' "
        )
        params = [*unique, *unique]
        if exclude:
            # The match on screen has both of them in it by definition.
            sql += "AND a.match_id != ? "
            params.append(exclude)
        out = {}
        for a, b, started_at in self.conn.execute(sql, params):
            out.setdefault((a, b), []).append(started_at)
        return out

    def party_evidence(self, puuids, exclude=None):
        """{(a, b): shared matches} - party_timeline counted rather than dated.

        Kept for callers that want the bare number: the match command prints it,
        and the offline checks lean on it. The live table takes the timeline.
        """
        timeline = self.party_timeline(puuids, exclude=exclude)
        return {pair: len(times) for pair, times in timeline.items()}

    def mates_of(self, puuid, since=None):
        """{other puuid: [match times]} for everyone who played on this one's side.

        The same evidence party_timeline gathers, asked about one player against
        the whole cache rather than about one lobby. Riot publishes nobody's
        friends list, so this is the nearest thing there is to one: not who they
        added, but who they keep turning up next to.
        """
        if not self.conn or not puuid:
            return {}
        sql = (
            "SELECT b.puuid, m.started_at FROM match_perf a "
            "JOIN match_perf b ON b.match_id = a.match_id "
            "AND b.team = a.team AND b.puuid != a.puuid "
            "LEFT JOIN matches m ON m.match_id = a.match_id "
            "WHERE a.puuid = ? AND a.team IS NOT NULL AND a.team != '' "
        )
        params = [puuid]
        if since:
            # A match with no recorded time cannot be placed, so it is dropped
            # rather than quietly counted as if it were inside the window.
            sql += "AND m.started_at IS NOT NULL AND m.started_at >= ? "
            params.append(since)
        out = {}
        for other, started_at in self.conn.execute(sql, params):
            out.setdefault(other, []).append(started_at)
        return out

    def team_samples(self, puuids):
        """puuid -> how many cached matches we know their side in.

        A pair can only share what both of them have cached, so this is what
        says whether "no shared matches" means anything at all.
        """
        unique = sorted({p for p in (puuids or ()) if p})
        if not self.conn or not unique:
            return {}
        marks = ",".join("?" * len(unique))
        cur = self.conn.execute(
            f"SELECT puuid, COUNT(*) FROM match_perf WHERE puuid IN ({marks}) "
            "AND team IS NOT NULL AND team != '' GROUP BY puuid",
            unique,
        )
        return dict(cur.fetchall())

    def shared_match_rows(self, puuids):
        """Every same-side match among `puuids`, with each of their lines in it.

        This is party_timeline with the evidence attached rather than counted:
        the same-side matches, the map and queue and kickoff of each, the local
        M-number, and the full per-match line of every member who played in it.
        One query, no requests - it is the cache the form numbers already
        filled, asked to show its working.

        Comes back newest first as [(match, {puuid: line})], where a member who
        was not in that match simply has no entry. The report leans on that:
        a row where only two of three members appear is exactly the case that
        makes a three-stack a chain rather than a group.
        """
        unique = sorted({p for p in (puuids or ()) if p})
        if not self.conn or len(unique) < 2:
            return []
        marks = ",".join("?" * len(unique))
        cur = self.conn.execute(
            f"""
            SELECT DISTINCT a.match_id FROM match_perf a
            JOIN match_perf b ON b.match_id = a.match_id AND b.team = a.team
                             AND b.puuid > a.puuid
            WHERE a.puuid IN ({marks}) AND b.puuid IN ({marks})
              AND a.team IS NOT NULL AND a.team != ''
            """,
            [*unique, *unique],
        )
        ids = [row[0] for row in cur.fetchall()]
        if not ids:
            return []

        id_marks = ",".join("?" * len(ids))
        cur = self.conn.execute(
            f"""
            SELECT m.match_id, l.num, m.map_id, m.queue, m.started_at, m.score, m.length
            FROM matches m LEFT JOIN match_labels l ON l.match_id = m.match_id
            WHERE m.match_id IN ({id_marks})
            ORDER BY COALESCE(m.started_at, '') DESC
            """,
            ids,
        )
        fields = ("match_id", "num", "map_id", "queue", "started_at", "score", "length")
        matches = [dict(zip(fields, row)) for row in cur.fetchall()]
        # A match nobody recorded the meta for still has lines worth showing,
        # so it comes back with blanks rather than being dropped on the join.
        known = {match["match_id"] for match in matches}
        matches += [dict(zip(fields, (mid, None, "", "", None, "", 0))) for mid in ids if mid not in known]

        columns = ", ".join(PERF_FIELDS)
        cur = self.conn.execute(
            f"SELECT match_id, puuid, {columns} FROM match_perf "
            f"WHERE match_id IN ({id_marks}) AND puuid IN ({marks})",
            [*ids, *unique],
        )
        lines = {}
        for row in cur.fetchall():
            lines.setdefault(row[0], {})[row[1]] = dict(zip(PERF_FIELDS, row[2:]))
        return [(match, lines.get(match["match_id"], {})) for match in matches]

    # ---------------------------------------------------------------- conduct

    def store_conduct(self, match_id, per_player):
        """Behaviour rows for one match. Replaces: a match record cannot change."""
        if not self.conn or not match_id or not per_player:
            return
        columns = ", ".join(conduct_module.FIELDS)
        marks = ", ".join("?" * len(conduct_module.FIELDS))
        self.conn.executemany(
            f"INSERT OR REPLACE INTO match_conduct (match_id, puuid, {columns}) "
            f"VALUES (?, ?, {marks})",
            [
                (match_id, puuid, *[row.get(field) or 0 for field in conduct_module.FIELDS])
                for puuid, row in per_player.items()
            ],
        )
        self._commit()

    def conduct_for(self, puuid, limit=30):
        """One player's cached conduct rows, newest match first."""
        if not self.conn or not puuid:
            return []
        columns = ", ".join(f"c.{field}" for field in conduct_module.FIELDS)
        cur = self.conn.execute(
            f"SELECT {columns}, c.match_id, m.started_at, m.queue FROM match_conduct c "
            "LEFT JOIN matches m ON m.match_id = c.match_id WHERE c.puuid = ? "
            "ORDER BY COALESCE(m.started_at, '') DESC LIMIT ?",
            (puuid, int(limit)),
        )
        fields = (*conduct_module.FIELDS, "match_id", "started_at", "queue")
        return [dict(zip(fields, row)) for row in cur.fetchall()]

    # ------------------------------------------------------------ rr updates

    # The database spells these in its own words; mmr.py reads Riot's. The two
    # maps below are the only place the translation happens.
    _RR_COLUMNS = (
        "tier_before",
        "tier_after",
        "rr_before",
        "rr_after",
        "earned",
        "bonus",
        "afk_penalty",
        "rr_penalty",
        "refund",
        "map_bonus",
        "placement",
        "derank_protected",
    )
    _RR_FROM_RIOT = (
        ("tier_before", "TierBeforeUpdate"),
        ("tier_after", "TierAfterUpdate"),
        ("rr_before", "RankedRatingBeforeUpdate"),
        ("rr_after", "RankedRatingAfterUpdate"),
        ("earned", "RankedRatingEarned"),
        ("bonus", "RankedRatingPerformanceBonus"),
        ("afk_penalty", "AFKPenalty"),
        ("rr_penalty", "RRPenalty"),
        ("refund", "RankedRatingRefundApplied"),
        ("map_bonus", "NewMapIncentiveRRForgiven"),
        ("placement", "IsPlacementMatch"),
        ("derank_protected", "WasDerankProtected"),
    )

    def store_rr_updates(self, puuid, matches):
        """Ranked RR history for one player, as competitiveupdates served it."""
        if not self.conn or not puuid or not matches:
            return
        columns = ", ".join(self._RR_COLUMNS)
        marks = ", ".join("?" * len(self._RR_COLUMNS))
        rows = []
        for entry in matches:
            match_id = entry.get("MatchID")
            if not match_id:
                continue
            values = []
            for column, key in self._RR_FROM_RIOT:
                raw = entry.get(key)
                # RRPenalty arrives as a fraction of the RR withheld; it is
                # kept in whole hundredths so the column can stay an integer.
                values.append(_hundredths(raw) if column == "rr_penalty" else _whole(raw))
            rows.append((puuid, match_id, _millis(entry.get("MatchStartTime")), *values))
        if not rows:
            return
        self.conn.executemany(
            f"INSERT OR REPLACE INTO rr_updates (puuid, match_id, ts, {columns}) "
            f"VALUES (?, ?, ?, {marks})",
            rows,
        )
        self._commit()

    def rr_updates_for(self, puuid, limit=30):
        """One player's RR history, newest first, in the shape mmr.read wants."""
        if not self.conn or not puuid:
            return []
        columns = ", ".join(self._RR_COLUMNS)
        cur = self.conn.execute(
            f"SELECT match_id, ts, {columns} FROM rr_updates WHERE puuid = ? "
            "ORDER BY COALESCE(ts, '') DESC LIMIT ?",
            (puuid, int(limit)),
        )
        out = []
        for row in cur.fetchall():
            entry = {"MatchID": row[0], "StoredAt": row[1]}
            for (column, key), value in zip(self._RR_FROM_RIOT, row[2:]):
                entry[key] = value / 100.0 if column == "rr_penalty" else value
            out.append(entry)
        return out

    def side_strengths(self, min_ranked=3):
        """[(difference in RR, did the first side win)] over cached matches.

        What odds.fit() is fitted against: every cached match where both sides
        carried enough ranked players to average, together with which of them
        won. The rank comes from rank_snapshots, so it is whatever the match
        record said each player's tier was that evening - a division's
        granularity, no RR - which is blunt but is the only account of a side's
        strength that survives in a cache rather than being asked for again.

        The sides are ordered by their team name, so the sign of the difference
        means the same thing in every row.
        """
        if not self.conn:
            return []
        cur = self.conn.execute(
            """
            SELECT p.match_id, p.team,
                   COUNT(r.tier) AS ranked,
                   AVG(r.tier * 100 + r.rr) AS position,
                   MAX(p.won) AS won
            FROM match_perf p
            JOIN rank_snapshots r
              ON r.puuid = p.puuid AND r.match_id = p.match_id AND r.tier > 0
            WHERE p.team <> ''
            GROUP BY p.match_id, p.team
            HAVING COUNT(r.tier) >= ?
            """,
            (int(min_ranked),),
        )
        sides = {}
        for match_id, team, _ranked, position, won in cur.fetchall():
            sides.setdefault(match_id, []).append((team, position, won))
        out = []
        for entries in sides.values():
            if len(entries) != 2:
                continue
            entries.sort()
            (_, first, first_won), (_, second, second_won) = entries
            # One winner, or the match is not readable as a match.
            if bool(first_won) == bool(second_won):
                continue
            out.append((first - second, bool(first_won)))
        return out

    def ranked_positions(self):
        """{puuid: their mean rank position in RR} over every cached snapshot."""
        if not self.conn:
            return {}
        cur = self.conn.execute(
            "SELECT puuid, AVG(tier * 100 + rr) FROM rank_snapshots WHERE tier > 0 GROUP BY puuid"
        )
        return {puuid: position for puuid, position in cur.fetchall() if position}

    def rr_histories(self, min_matches=16, limit=400):
        """{puuid: their whole RR history} for everybody deep enough to read.

        What mmr.calibrate() measures the convergence constant against. The
        depth filter is done in SQL rather than in Python because most cached
        players have two or three ranked matches to their name and pulling all
        of them back to throw them away is the one slow way to ask this.

        Newest first inside each player, which is the order mmr.read() and
        mmr._one() both expect to have to sort for themselves.
        """
        if not self.conn:
            return {}
        columns = ", ".join(self._RR_COLUMNS)
        cur = self.conn.execute(
            f"SELECT puuid, match_id, ts, {columns} FROM rr_updates WHERE puuid IN ("
            "  SELECT puuid FROM rr_updates GROUP BY puuid HAVING COUNT(*) >= ?"
            "  ORDER BY COUNT(*) DESC LIMIT ?"
            ") ORDER BY puuid, COALESCE(ts, '') DESC",
            (int(min_matches), int(limit)),
        )
        out = {}
        for row in cur.fetchall():
            entry = {"MatchID": row[1], "StoredAt": row[2]}
            for (column, key), value in zip(self._RR_FROM_RIOT, row[3:]):
                entry[key] = value / 100.0 if column == "rr_penalty" else value
            out.setdefault(row[0], []).append(entry)
        return out

    # ------------------------------------------------------------- population

    def population(self, min_rounds=40):
        """One weighted summary per cached player, for calibrating the score.

        Same weighting as perf.summarise(): totals divided once, not an average
        of averages. Players with barely any rounds are left out entirely.
        """
        if not self.conn:
            return []
        cur = self.conn.execute(
            """
            SELECT puuid,
                   SUM(score)   AS score,
                   SUM(rounds)  AS rounds,
                   SUM(kast)    AS kast,
                   SUM(dmg_dealt) - SUM(dmg_received) AS delta,
                   SUM(won)     AS wins,
                   COUNT(*)     AS matches
            FROM match_perf
            GROUP BY puuid
            HAVING SUM(rounds) >= ?
            """,
            (min_rounds,),
        )
        out = []
        for puuid, score, rounds, kast, delta, wins, matches in cur.fetchall():
            out.append(
                {
                    "puuid": puuid,
                    "acs": score / rounds,
                    "kast": 100.0 * kast / rounds,
                    "dd": delta / rounds,
                    "winrate": 100.0 * wins / matches,
                    "rounds": rounds,
                    "matches": matches,
                }
            )
        return out

    def calibration(self):
        """(stored blob, how many players it was built from)."""
        if not self.conn:
            return None, 0
        cur = self.conn.execute("SELECT data, players FROM calibration WHERE id = 1")
        row = cur.fetchone()
        return (row[0], row[1]) if row else (None, 0)

    def store_calibration(self, blob, players):
        if not self.conn:
            return
        self.conn.execute(
            "INSERT OR REPLACE INTO calibration (id, players, ts, data) VALUES (1, ?, ?, ?)",
            (players, _now(), blob),
        )
        self.conn.commit()

    # ------------------------------------------------------------- lookups

    def find(self, query, limit=10):
        """Players whose remembered name contains `query`, most-met first."""
        if not self.conn:
            return []
        cur = self.conn.execute(
            "SELECT puuid, name, times, first_seen, last_seen FROM encounters "
            "WHERE name LIKE ? ORDER BY times DESC, last_seen DESC LIMIT ?",
            (f"%{query}%", limit),
        )
        return [
            dict(zip(("puuid", "name", "times", "first_seen", "last_seen"), row))
            for row in cur.fetchall()
        ]

    def named_players(self):
        """Everyone we have a name for, for the completer to search through.

        The whole table at once, on purpose: matching a half-typed name against
        it is fuzzy, and fuzzy is not something SQLite can do for us. A few
        thousand rows of three fields is nothing to hold in memory, and it is
        read once when the program starts rather than on every keystroke.
        """
        if not self.conn:
            return []
        cur = self.conn.execute(
            "SELECT puuid, name, times, first_seen, last_seen FROM encounters "
            "WHERE COALESCE(name, '') != '' ORDER BY times DESC, last_seen DESC"
        )
        return [
            dict(zip(("puuid", "name", "times", "first_seen", "last_seen"), row))
            for row in cur.fetchall()
        ]

    def match_info(self, match_id):
        """Map, queue, kickoff time and local number of one match by its uuid.

        match_by_label answers the same question from the other end - there the
        number is what you have, here the uuid is.
        """
        if not self.conn or not match_id:
            return None
        cur = self.conn.execute(
            """
            SELECT l.num, m.match_id, COALESCE(m.started_at, l.ts, s.ts), m.map_id, m.queue
            FROM matches m
            LEFT JOIN match_labels l ON l.match_id = m.match_id
            LEFT JOIN seen_matches s ON s.match_id = m.match_id
            WHERE m.match_id = ?
            """,
            (match_id,),
        )
        row = cur.fetchone()
        if row:
            return dict(zip(("num", "match_id", "ts", "map_id", "queue"), row))
        # A match can be in the encounter log without ever having been parsed.
        cur = self.conn.execute(
            """
            SELECT l.num, l.match_id, COALESCE(l.ts, s.ts), '', ''
            FROM match_labels l
            LEFT JOIN seen_matches s ON s.match_id = l.match_id
            WHERE l.match_id = ?
            """,
            (match_id,),
        )
        row = cur.fetchone()
        return dict(zip(("num", "match_id", "ts", "map_id", "queue"), row)) if row else None

    def match_lines(self, match_id):
        """Every cached line from one match, with names and ranks where we have them.

        This is the scoreboard as the local memory remembers it: one row per
        player the match record carried, which is all ten of them, plus the
        Riot ID for the ones we have since put a name to and the rank each was
        at that evening.
        """
        if not self.conn or not match_id:
            return []
        columns = ", ".join(f"p.{field}" for field in PERF_FIELDS)
        cur = self.conn.execute(
            f"""
            SELECT p.puuid, {columns}, c.name, r.tier, r.rr
            FROM match_perf p
            LEFT JOIN encounters c ON c.puuid = p.puuid
            LEFT JOIN rank_snapshots r ON r.puuid = p.puuid AND r.match_id = p.match_id
            WHERE p.match_id = ?
            """,
            (match_id,),
        )
        fields = ("puuid", *PERF_FIELDS, "name", "tier", "rr")
        return [dict(zip(fields, row)) for row in cur.fetchall()]

    def most_met(self, limit=20):
        if not self.conn:
            return []
        cur = self.conn.execute(
            "SELECT puuid, name, times, first_seen, last_seen FROM encounters "
            "ORDER BY times DESC, last_seen DESC LIMIT ?",
            (limit,),
        )
        return [
            dict(zip(("puuid", "name", "times", "first_seen", "last_seen"), row))
            for row in cur.fetchall()
        ]

    def snapshots(self, puuid):
        """Their rank each time you met them, oldest first."""
        if not self.conn:
            return []
        cur = self.conn.execute(
            "SELECT ts, tier, rr FROM rank_snapshots WHERE puuid = ? ORDER BY ts", (puuid,)
        )
        return [dict(zip(("ts", "tier", "rr"), row)) for row in cur.fetchall()]

    def totals(self):
        """Rough size of the local memory, for the CLI."""
        if not self.conn:
            return {"players": 0, "matches": 0, "lines": 0}
        one = lambda sql: self.conn.execute(sql).fetchone()[0]  # noqa: E731
        return {
            "players": one("SELECT COUNT(*) FROM encounters"),
            "matches": one("SELECT COUNT(*) FROM parsed_matches"),
            "lines": one("SELECT COUNT(*) FROM match_perf"),
        }

    def close(self):
        if self.conn:
            self.conn.close()
            self.conn = None
