"""SQLite storage layer.

The database is the system of record: feeds, episode state, and speaker-attributed
turns with full-text search. Audio and raw model output live on disk beside it.

Every public method takes and returns the plain types in models.py, never rows,
so another backend only has to provide these same methods.
"""

from __future__ import annotations

import hashlib
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Iterator, Sequence

from .models import Correction, Episode, Feed, Hit, Turn

SCHEMA_VERSION = 3

SCHEMA = """
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
    source_guid TEXT NOT NULL,
    title       TEXT,
    summary     TEXT,
    published   TEXT,
    audio_url   TEXT NOT NULL,
    source_url  TEXT,
    audio_path  TEXT,
    duration    REAL,
    status      TEXT NOT NULL DEFAULT 'new',
    error       TEXT,
    updated_at  TEXT NOT NULL,
    revision    INTEGER NOT NULL DEFAULT 0,
    merged_at   TEXT
);

CREATE INDEX IF NOT EXISTS idx_episodes_status ON episodes(status);

CREATE TABLE IF NOT EXISTS changes (
    seq          INTEGER PRIMARY KEY AUTOINCREMENT,
    episode_guid TEXT NOT NULL,
    revision     INTEGER NOT NULL,
    kind         TEXT NOT NULL CHECK (kind IN ('transcript', 'metadata', 'deleted')),
    occurred_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_changes_seq ON changes(seq);

CREATE TABLE IF NOT EXISTS episode_tombstones (
    guid     TEXT PRIMARY KEY,
    revision INTEGER NOT NULL
);

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


def public_guid(feed_url: str, source_guid: str) -> str:
    """Globally stable episode key. A feed's own guids are only unique within that
    feed (two feeds can both use "1"), so scope them by the feed's URL."""
    return hashlib.sha1(f"{feed_url}\0{source_guid}".encode()).hexdigest()[:20]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _published_iso(value: str) -> str | None:
    """Normalize dates stored by versions that kept the feed's raw text."""
    iso = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(iso)
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


class Store:
    def __init__(self, path: Path):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        version = self.conn.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise RuntimeError(
                f"database schema {version} is newer than supported schema {SCHEMA_VERSION}"
            )
        if version < SCHEMA_VERSION:
            self.conn.execute("PRAGMA journal_mode = WAL")
            self.conn.executescript(SCHEMA)
            self._migrate(version)
            self.conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            self.conn.commit()

    def _migrate(self, version: int) -> None:
        """Bring a database created by an older version up to date."""
        if version < 1:
            cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(episodes)")}
            for name, ddl in (
                ("source_url", "TEXT"),
                ("summary", "TEXT"),
                ("revision", "INTEGER NOT NULL DEFAULT 0"),
                ("merged_at", "TEXT"),
                ("source_guid", "TEXT"),
            ):
                if name not in cols:
                    self.conn.execute(f"ALTER TABLE episodes ADD COLUMN {name} {ddl}")
            # Before source_guid existed, `guid` was the feed's own id. Keep it as the
            # public guid so existing caches and consumers keep working; only episodes
            # added from now on get a derived one.
            self.conn.execute("UPDATE episodes SET source_guid = guid WHERE source_guid IS NULL")
            self.conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_episodes_feed_source "
                "ON episodes(feed_id, source_guid)"
            )
            for row in self.conn.execute(
                "SELECT id, published FROM episodes WHERE published IS NOT NULL"
            ).fetchall():
                published = _published_iso(row["published"])
                if published != row["published"]:
                    self.conn.execute(
                        "UPDATE episodes SET published = ? WHERE id = ?", (published, row["id"])
                    )

        if version < 2:
            table = self.conn.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'changes'"
            ).fetchone()
            if table and "'deleted'" not in table["sql"]:
                self.conn.executescript(
                    """
                    CREATE TABLE changes_v2 (
                        seq          INTEGER PRIMARY KEY AUTOINCREMENT,
                        episode_guid TEXT NOT NULL,
                        revision     INTEGER NOT NULL,
                        kind         TEXT NOT NULL CHECK (kind IN ('transcript', 'metadata', 'deleted')),
                        occurred_at  TEXT NOT NULL
                    );
                    INSERT INTO changes_v2 SELECT * FROM changes;
                    DROP INDEX IF EXISTS idx_changes_seq;
                    DROP TABLE changes;
                    ALTER TABLE changes_v2 RENAME TO changes;
                    CREATE INDEX idx_changes_seq ON changes(seq);
                    """
                )

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def snapshot(self) -> Iterator[None]:
        """One consistent view for several reads. In WAL mode a read transaction
        sees the database as of its first read, however much others commit meanwhile."""
        self.conn.execute("BEGIN")
        try:
            yield
        finally:
            self.conn.rollback()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        with self.conn:
            yield self.conn

    @contextmanager
    def processing_lock(self) -> Iterator[bool]:
        """Take the exclusive process-wide lock for model processing.

        The lock lives beside the database rather than inside a SQLite
        transaction, so a long transcription run does not hold up the
        processor's own database writes. The kernel releases it if the owner
        exits unexpectedly; the empty lock file itself is harmless.
        """
        from fcntl import LOCK_EX, LOCK_NB, LOCK_UN, flock

        lock_path = self.path.with_name(self.path.name + ".processing.lock")
        with lock_path.open("a") as handle:
            try:
                flock(handle.fileno(), LOCK_EX | LOCK_NB)
            except BlockingIOError:
                yield False
                return
            try:
                yield True
            finally:
                flock(handle.fileno(), LOCK_UN)

    # ---- feeds -----------------------------------------------------------

    def add_feed(self, url: str, title: str | None = None) -> Feed:
        with self.tx() as c:
            existing = c.execute("SELECT id, title FROM feeds WHERE url = ?", (url,)).fetchone()
            c.execute(
                "INSERT OR IGNORE INTO feeds (url, title, added_at) VALUES (?, ?, ?)",
                (url, title, _now()),
            )
            if title and existing and title != existing["title"]:
                c.execute("UPDATE feeds SET title = ? WHERE url = ?", (title, url))
                for row in c.execute(
                    "SELECT guid, revision FROM episodes WHERE feed_id = ?", (existing["id"],)
                ):
                    self._insert_change(c, row["guid"], row["revision"], "metadata")
        row = self.conn.execute("SELECT * FROM feeds WHERE url = ?", (url,)).fetchone()
        return _feed(row)

    def feeds(self) -> list[Feed]:
        return [_feed(r) for r in self.conn.execute("SELECT * FROM feeds ORDER BY id")]

    def feed(self, feed_id: int) -> Feed:
        row = self.conn.execute("SELECT * FROM feeds WHERE id = ?", (feed_id,)).fetchone()
        if row is None:
            raise LookupError(f"no feed {feed_id}")
        return _feed(row)

    def update_feed(self, feed_id: int, url: str, title: str | None = None) -> Feed:
        with self.tx() as c:
            existing = c.execute(
                "SELECT url, title FROM feeds WHERE id = ?", (feed_id,)
            ).fetchone()
            if existing is None:
                raise LookupError(f"no feed {feed_id}")
            conflict = c.execute(
                "SELECT id FROM feeds WHERE url = ? AND id != ?", (url, feed_id)
            ).fetchone()
            if conflict is not None:
                raise ValueError("url already registered")
            if existing["url"] != url or existing["title"] != title:
                c.execute(
                    "UPDATE feeds SET url = ?, title = ? WHERE id = ?",
                    (url, title, feed_id),
                )
                for row in c.execute(
                    "SELECT guid, revision FROM episodes WHERE feed_id = ?", (feed_id,)
                ):
                    self._insert_change(c, row["guid"], row["revision"], "metadata")
        return self.feed(feed_id)

    def delete_feed(self, feed_id: int) -> list[Episode]:
        with self.tx() as c:
            if not c.execute("SELECT 1 FROM feeds WHERE id = ?", (feed_id,)).fetchone():
                raise LookupError(f"no feed {feed_id}")
            rows = c.execute("SELECT * FROM episodes WHERE feed_id = ?", (feed_id,)).fetchall()
            for row in rows:
                c.execute(
                    "INSERT INTO episode_tombstones (guid, revision) VALUES (?, ?) "
                    "ON CONFLICT(guid) DO UPDATE SET revision = MAX(revision, excluded.revision)",
                    (row["guid"], row["revision"]),
                )
                self._insert_change(c, row["guid"], row["revision"], "deleted")
            episode_ids = [row["id"] for row in rows]
            if episode_ids:
                c.executemany("DELETE FROM turns_fts WHERE episode_id = ?", [(i,) for i in episode_ids])
            c.execute("DELETE FROM feeds WHERE id = ?", (feed_id,))
            return [_episode(row) for row in rows]

    # ---- episodes --------------------------------------------------------

    def add_episode(
        self,
        feed_id: int,
        source_guid: str,
        title: str | None,
        published: str | None,
        audio_url: str,
        source_url: str | None = None,
        summary: str | None = None,
    ) -> bool:
        """Record an episode from a feed. Returns True if it was new.

        A known episode (same feed and source guid) is refreshed instead: title,
        date and audio URL are updated when the feed now says something different,
        so a corrected or re-hosted enclosure reaches the next retry. A value the
        feed no longer provides never erases the stored one.
        """
        with self.tx() as c:
            row = c.execute(
                "SELECT id, title, summary, published, audio_url, source_url, guid, revision FROM episodes "
                "WHERE feed_id = ? AND source_guid = ?",
                (feed_id, source_guid),
            ).fetchone()
            if row is None:
                feed_url = c.execute("SELECT url FROM feeds WHERE id = ?", (feed_id,)).fetchone()["url"]
                guid = public_guid(feed_url, source_guid)
                tombstone = c.execute(
                    "SELECT revision FROM episode_tombstones WHERE guid = ?", (guid,)
                ).fetchone()
                revision = tombstone["revision"] + 1 if tombstone else 0
                c.execute(
                    """INSERT INTO episodes
                       (feed_id, guid, source_guid, title, summary, published, audio_url,
                        source_url, status, updated_at, revision)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (feed_id, guid, source_guid, title, summary, published, audio_url,
                     source_url, STATUS_NEW, _now(), revision),
                )
                self._insert_change(c, guid, revision, "metadata")
                return True

            new = {
                "title": title,
                "summary": summary,
                "published": published,
                "audio_url": audio_url,
                "source_url": source_url,
            }
            changed = {k: v for k, v in new.items() if v is not None and v != row[k]}
            if changed:
                sets = ", ".join(f"{k} = ?" for k in changed)
                c.execute(
                    f"UPDATE episodes SET {sets}, updated_at = ? WHERE id = ?",
                    [*changed.values(), _now(), row["id"]],
                )
                self._insert_change(c, row["guid"], row["revision"], "metadata")
            return False

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
        self,
        status: str | None = None,
        updated_since: str | None = None,
        after_id: int = 0,
        limit: int | None = None,
    ) -> list[Episode]:
        """Episodes in id order. `updated_since` is an ISO timestamp, inclusive."""
        where, params = [], []
        if after_id:
            where.append("id > ?")
            params.append(after_id)
        if status:
            where.append("status = ?")
            params.append(status)
        if updated_since:
            where.append("updated_at >= ?")
            params.append(updated_since)
        sql = "SELECT * FROM episodes"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY id"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        return [_episode(r) for r in self.conn.execute(sql, params)]

    def source_guids(self, feed_id: int) -> set[str]:
        return {
            row["source_guid"]
            for row in self.conn.execute(
                "SELECT source_guid FROM episodes WHERE feed_id = ?", (feed_id,)
            )
        }

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

    def clear_audio(self, episode_id: int) -> None:
        """Forget the source audio path, after the file has been deleted."""
        with self.tx() as c:
            c.execute(
                "UPDATE episodes SET audio_path = NULL, updated_at = ? WHERE id = ?",
                (_now(), episode_id),
            )

    # ---- transcripts -----------------------------------------------------

    def replace_turns(
        self,
        episode_id: int,
        turns: Sequence[Turn],
        corrections: Sequence[Correction] = (),
    ) -> None:
        """Swap in a new transcript, bump the episode's revision and mark it done,
        all in one transaction: a reader never sees a transcript on an episode that
        isn't done, and a failure part-way leaves everything as it was."""
        with self.tx() as c:
            now = _now()
            c.execute(
                "UPDATE episodes SET revision = revision + 1, merged_at = ?, updated_at = ?, "
                "status = ?, error = NULL WHERE id = ?",
                (now, now, STATUS_DONE, episode_id),
            )
            episode = c.execute(
                "SELECT guid, revision FROM episodes WHERE id = ?", (episode_id,)
            ).fetchone()
            if episode is None:
                raise LookupError(f"no episode {episode_id}")
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
            self._insert_change(c, episode["guid"], episode["revision"], "transcript")

    @staticmethod
    def _insert_change(c: sqlite3.Connection, guid: str, revision: int, kind: str) -> None:
        c.execute(
            "INSERT INTO changes (episode_guid, revision, kind, occurred_at) VALUES (?, ?, ?, ?)",
            (guid, revision, kind, _now()),
        )

    def highest_change_seq(self) -> int:
        row = self.conn.execute("SELECT COALESCE(MAX(seq), 0) AS seq FROM changes").fetchone()
        return int(row["seq"])

    def changes(self, after: int = 0, limit: int = 100) -> list[dict]:
        rows = self.conn.execute(
            """SELECT seq, episode_guid, revision, kind, occurred_at
               FROM changes WHERE seq > ? ORDER BY seq LIMIT ?""",
            (after, limit),
        )
        return [dict(r) for r in rows]

    def emit_current(self) -> int:
        """Queue the current revision of every completed episode for bootstrap."""
        with self.tx() as c:
            rows = c.execute(
                "SELECT guid, revision FROM episodes WHERE status = ? ORDER BY id",
                (STATUS_DONE,),
            ).fetchall()
            for row in rows:
                self._insert_change(c, row["guid"], row["revision"], "transcript")
            return len(rows)

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
