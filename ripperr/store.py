"""SQLite storage layer.

The database is the system of record: feeds, episode state, and speaker-attributed
turns with full-text search. Audio and raw model output live on disk beside it.

Every public method takes and returns the plain types in models.py, never rows,
so another backend only has to provide these same methods.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Sequence

from .models import Correction, Episode, Feed, Hit, Turn

SCHEMA = """
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS feeds (
    id        INTEGER PRIMARY KEY,
    url       TEXT NOT NULL UNIQUE,
    title     TEXT,
    added_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS episodes (
    id          INTEGER PRIMARY KEY,
    feed_id     INTEGER NOT NULL REFERENCES feeds(id) ON DELETE CASCADE,
    guid        TEXT NOT NULL UNIQUE,
    title       TEXT,
    published   TEXT,
    audio_url   TEXT NOT NULL,
    audio_path  TEXT,
    duration    REAL,
    status      TEXT NOT NULL DEFAULT 'new',
    error       TEXT,
    updated_at  TEXT NOT NULL,
    revision    INTEGER NOT NULL DEFAULT 0,
    merged_at   TEXT
);

CREATE INDEX IF NOT EXISTS idx_episodes_status ON episodes(status);

CREATE TABLE IF NOT EXISTS turns (
    id          INTEGER PRIMARY KEY,
    episode_id  INTEGER NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
    idx         INTEGER NOT NULL,
    speaker     TEXT NOT NULL,
    start       REAL NOT NULL,
    end         REAL NOT NULL,
    text        TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_turns_episode ON turns(episode_id, idx);

CREATE TABLE IF NOT EXISTS corrections (
    episode_id  INTEGER NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
    heard       TEXT NOT NULL,
    fixed       TEXT NOT NULL,
    count       INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_corrections_episode ON corrections(episode_id);

CREATE VIRTUAL TABLE IF NOT EXISTS turns_fts USING fts5(
    text,
    turn_id     UNINDEXED,
    episode_id  UNINDEXED
);
"""

# Episode lifecycle
STATUS_NEW = "new"
STATUS_DOWNLOADED = "downloaded"
STATUS_DONE = "done"
STATUS_ERROR = "error"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        """Bring a database created by an older version up to date."""
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(episodes)")}
        for name, ddl in (("revision", "INTEGER NOT NULL DEFAULT 0"), ("merged_at", "TEXT")):
            if name not in cols:
                self.conn.execute(f"ALTER TABLE episodes ADD COLUMN {name} {ddl}")

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        with self.conn:
            yield self.conn

    # ---- feeds -----------------------------------------------------------

    def add_feed(self, url: str, title: str | None = None) -> Feed:
        with self.tx() as c:
            c.execute(
                "INSERT OR IGNORE INTO feeds (url, title, added_at) VALUES (?, ?, ?)",
                (url, title, _now()),
            )
            if title:
                c.execute("UPDATE feeds SET title = ? WHERE url = ?", (title, url))
        row = self.conn.execute("SELECT * FROM feeds WHERE url = ?", (url,)).fetchone()
        return _feed(row)

    def feeds(self) -> list[Feed]:
        return [_feed(r) for r in self.conn.execute("SELECT * FROM feeds ORDER BY id")]

    # ---- episodes --------------------------------------------------------

    def add_episode(
        self,
        feed_id: int,
        guid: str,
        title: str | None,
        published: str | None,
        audio_url: str,
    ) -> bool:
        """Returns True if this episode was new to us."""
        with self.tx() as c:
            cur = c.execute(
                """INSERT OR IGNORE INTO episodes
                   (feed_id, guid, title, published, audio_url, status, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (feed_id, guid, title, published, audio_url, STATUS_NEW, _now()),
            )
            return cur.rowcount > 0

    def pending(self, limit: int | None = None, retry_errors: bool = False) -> list[Episode]:
        sql = (
            "SELECT * FROM episodes WHERE status IN (?, ?, ?) ORDER BY published DESC, id DESC"
        )
        params: list[object] = [STATUS_NEW, STATUS_DOWNLOADED, STATUS_ERROR if retry_errors else STATUS_NEW]
        if limit:
            sql += " LIMIT ?"
            params.append(limit)
        return [_episode(r) for r in self.conn.execute(sql, params)]

    def episode(self, guid: str) -> Episode | None:
        row = self.conn.execute("SELECT * FROM episodes WHERE guid = ?", (guid,)).fetchone()
        return _episode(row) if row else None

    def episode_by_id(self, episode_id: int) -> Episode | None:
        row = self.conn.execute("SELECT * FROM episodes WHERE id = ?", (episode_id,)).fetchone()
        return _episode(row) if row else None

    def episodes(
        self, status: str | None = None, updated_since: str | None = None
    ) -> list[Episode]:
        """Episodes in id order. `updated_since` is an ISO timestamp, inclusive."""
        where, params = [], []
        if status:
            where.append("status = ?")
            params.append(status)
        if updated_since:
            where.append("updated_at >= ?")
            params.append(updated_since)
        sql = "SELECT * FROM episodes"
        if where:
            sql += " WHERE " + " AND ".join(where)
        return [_episode(r) for r in self.conn.execute(sql + " ORDER BY id", params)]

    def set_status(
        self,
        episode_id: int,
        status: str,
        *,
        error: str | None = None,
        audio_path: str | None = None,
        duration: float | None = None,
    ) -> None:
        sets = ["status = ?", "updated_at = ?", "error = ?"]
        params: list[object] = [status, _now(), error]
        if audio_path is not None:
            sets.append("audio_path = ?")
            params.append(audio_path)
        if duration is not None:
            sets.append("duration = ?")
            params.append(duration)
        params.append(episode_id)
        with self.tx() as c:
            c.execute(f"UPDATE episodes SET {', '.join(sets)} WHERE id = ?", params)

    # ---- transcripts -----------------------------------------------------

    def replace_turns(
        self,
        episode_id: int,
        turns: Sequence[Turn],
        corrections: Sequence[Correction] = (),
    ) -> None:
        """Swap in a new transcript and bump the episode's revision."""
        with self.tx() as c:
            now = _now()
            c.execute(
                "UPDATE episodes SET revision = revision + 1, merged_at = ?, updated_at = ? WHERE id = ?",
                (now, now, episode_id),
            )
            c.execute("DELETE FROM corrections WHERE episode_id = ?", (episode_id,))
            c.executemany(
                "INSERT INTO corrections (episode_id, heard, fixed, count) VALUES (?, ?, ?, ?)",
                [(episode_id, x.heard, x.fixed, x.count) for x in corrections],
            )
            old = [
                r["id"]
                for r in c.execute(
                    "SELECT id FROM turns WHERE episode_id = ?", (episode_id,)
                )
            ]
            if old:
                c.executemany(
                    "DELETE FROM turns_fts WHERE turn_id = ?", [(i,) for i in old]
                )
                c.execute("DELETE FROM turns WHERE episode_id = ?", (episode_id,))

            for t in turns:
                cur = c.execute(
                    """INSERT INTO turns (episode_id, idx, speaker, start, end, text)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (episode_id, t.idx, t.speaker, t.start, t.end, t.text),
                )
                c.execute(
                    "INSERT INTO turns_fts (text, turn_id, episode_id) VALUES (?, ?, ?)",
                    (t.text, cur.lastrowid, episode_id),
                )

    def turns(self, episode_id: int) -> list[Turn]:
        rows = self.conn.execute(
            "SELECT idx, speaker, start, end, text FROM turns WHERE episode_id = ? ORDER BY idx",
            (episode_id,),
        )
        return [Turn(**dict(r)) for r in rows]

    def corrections(self, episode_id: int) -> list[Correction]:
        rows = self.conn.execute(
            "SELECT heard, fixed, count FROM corrections WHERE episode_id = ? ORDER BY count DESC, heard",
            (episode_id,),
        )
        return [Correction(**dict(r)) for r in rows]

    def search(self, query: str, limit: int = 20) -> list[Hit]:
        try:
            return self._search(query, limit)
        except sqlite3.OperationalError:
            # Not valid FTS5 syntax (e.g. "don't", "a-b"): retry as a literal phrase.
            return self._search('"' + query.replace('"', '""') + '"', limit)

    def _search(self, query: str, limit: int) -> list[Hit]:
        rows = self.conn.execute(
            """SELECT t.episode_id, e.guid, e.title AS episode_title, t.speaker, t.start, t.end,
                      snippet(turns_fts, 0, '[', ']', ' … ', 12) AS snippet
               FROM turns_fts f
               JOIN turns t     ON t.id = f.turn_id
               JOIN episodes e  ON e.id = t.episode_id
               WHERE turns_fts MATCH ?
               ORDER BY rank
               LIMIT ?""",
            (query, limit),
        )
        return [Hit(**dict(r)) for r in rows]

    def stats(self) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT status, COUNT(*) AS n FROM episodes GROUP BY status"
        )
        return {r["status"]: r["n"] for r in rows}


def _feed(row: sqlite3.Row) -> Feed:
    return Feed(id=row["id"], url=row["url"], title=row["title"])


def _episode(row: sqlite3.Row) -> Episode:
    return Episode(**{k: row[k] for k in Episode.__dataclass_fields__})
