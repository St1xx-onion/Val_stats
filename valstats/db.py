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
from datetime import datetime, timezone
from pathlib import Path

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
    PRIMARY KEY (match_id, puuid)
);
CREATE INDEX IF NOT EXISTS match_perf_by_player ON match_perf (puuid);
CREATE TABLE IF NOT EXISTS parsed_matches (
    match_id TEXT PRIMARY KEY
);
CREATE TABLE IF NOT EXISTS rank_snapshots (
    puuid    TEXT NOT NULL,
    match_id TEXT NOT NULL,
    ts       TEXT,
    tier     INTEGER NOT NULL DEFAULT 0,
    rr       INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (puuid, match_id)
);
CREATE TABLE IF NOT EXISTS calibration (
    id      INTEGER PRIMARY KEY CHECK (id = 1),
    players INTEGER NOT NULL DEFAULT 0,
    ts      TEXT,
    data    TEXT
);
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
)


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Encounters:
    def __init__(self, enabled=True, path=DB_PATH):
        self.enabled = enabled
        self.conn = None
        if enabled:
            self.conn = sqlite3.connect(path)
            self._migrate()
            self.conn.executescript(SCHEMA)
            self.conn.commit()

    def _migrate(self):
        """An older cache is missing columns; refetching is cheaper than guessing."""
        try:
            columns = {row[1] for row in self.conn.execute("PRAGMA table_info(match_perf)")}
        except sqlite3.DatabaseError:
            return
        if columns and not set(PERF_FIELDS) <= columns:
            self.conn.executescript(
                "DROP TABLE IF EXISTS match_perf; DROP TABLE IF EXISTS parsed_matches;"
            )
        # first_seen arrived later and is worth keeping the old rows for.
        met = {row[1] for row in self.conn.execute("PRAGMA table_info(encounters)")}
        if met and "first_seen" not in met:
            self.conn.execute("ALTER TABLE encounters ADD COLUMN first_seen TEXT")
        self.conn.commit()

    def counts(self, puuids):
        """puuid -> how many previous matches we have logged them in."""
        if not self.conn or not puuids:
            return {}
        marks = ",".join("?" * len(puuids))
        cur = self.conn.execute(
            f"SELECT puuid, times FROM encounters WHERE puuid IN ({marks})", list(puuids)
        )
        return dict(cur.fetchall())

    def record(self, match_id, rows):
        """Count a match once, no matter how often we re-render it."""
        if not self.conn or not match_id:
            return
        cur = self.conn.execute("SELECT 1 FROM seen_matches WHERE match_id = ?", (match_id,))
        if cur.fetchone():
            return
        now = _now()
        self.conn.execute("INSERT INTO seen_matches (match_id, ts) VALUES (?, ?)", (match_id, now))
        for row in rows:
            if not row.puuid or row.is_self:
                continue
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
                (match_id, puuid, *[entry.get(f, 0) for f in PERF_FIELDS])
                for puuid, entry in per_player.items()
                if puuid
            ],
        )
        self.conn.execute("INSERT OR IGNORE INTO parsed_matches (match_id) VALUES (?)", (match_id,))
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

    def all_perf_rows(self, puuid):
        """Every cached line for one player, however many matches that is."""
        if not self.conn:
            return []
        columns = ", ".join(PERF_FIELDS)
        cur = self.conn.execute(
            f"SELECT {columns} FROM match_perf WHERE puuid = ?", (puuid,)
        )
        return [dict(zip(PERF_FIELDS, row)) for row in cur.fetchall()]

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
