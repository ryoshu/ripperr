"""SQLite storage layer.

The database is the system of record: feeds, episode state, and speaker-attributed
turns with full-text search. Audio and raw model output live on disk beside it.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator, Sequence

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
    updated_at  TEXT NOT NULL
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
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        with self.conn:
            yield self.conn

    # ---- feeds -----------------------------------------------------------

    def add_feed(self, url: str, title: str | None = None) -> int:
        with self.tx() as c:
            c.execute(
                "INSERT OR IGNORE INTO feeds (url, title, added_at) VALUES (?, ?, ?)",
                (url, title, _now()),
            )
            if title:
                c.execute("UPDATE feeds SET title = ? WHERE url = ?", (title, url))
        row = self.conn.execute("SELECT id FROM feeds WHERE url = ?", (url,)).fetchone()
        return int(row["id"])

    def feeds(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM feeds ORDER BY id").fetchall()

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

    def pending(self, limit: int | None = None) -> list[sqlite3.Row]:
        sql = (
            "SELECT * FROM episodes WHERE status IN (?, ?) ORDER BY published DESC, id DESC"
        )
        params: list[object] = [STATUS_NEW, STATUS_DOWNLOADED]
        if limit:
            sql += " LIMIT ?"
            params.append(limit)
        return self.conn.execute(sql, params).fetchall()

    def episode(self, episode_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM episodes WHERE id = ?", (episode_id,)
        ).fetchone()

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

    def replace_turns(self, episode_id: int, turns: Sequence[dict]) -> None:
        with self.tx() as c:
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

            for idx, t in enumerate(turns):
                cur = c.execute(
                    """INSERT INTO turns (episode_id, idx, speaker, start, end, text)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (episode_id, idx, t["speaker"], t["start"], t["end"], t["text"]),
                )
                c.execute(
                    "INSERT INTO turns_fts (text, turn_id, episode_id) VALUES (?, ?, ?)",
                    (t["text"], cur.lastrowid, episode_id),
                )

    def turns(self, episode_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM turns WHERE episode_id = ? ORDER BY idx", (episode_id,)
        ).fetchall()

    def search(self, query: str, limit: int = 20) -> list[sqlite3.Row]:
        return self.conn.execute(
            """SELECT t.id, t.episode_id, t.speaker, t.start, t.end,
                      e.title AS episode_title,
                      snippet(turns_fts, 0, '[', ']', ' … ', 12) AS snip
               FROM turns_fts f
               JOIN turns t     ON t.id = f.turn_id
               JOIN episodes e  ON e.id = t.episode_id
               WHERE turns_fts MATCH ?
               ORDER BY rank
               LIMIT ?""",
            (query, limit),
        ).fetchall()

    def stats(self) -> dict[str, int]:
        rows: Iterable[sqlite3.Row] = self.conn.execute(
            "SELECT status, COUNT(*) AS n FROM episodes GROUP BY status"
        )
        return {r["status"]: r["n"] for r in rows}
