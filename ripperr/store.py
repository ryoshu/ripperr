"""SQLite storage layer.

The database is the system of record: feeds, episode state, speaker-attributed
turns with full-text search, and timestamped ad classifications. Audio and raw
model output live on disk beside it.

Every public method takes and returns the plain types in models.py, never rows,
so another backend only has to provide these same methods.
"""

from __future__ import annotations

import hashlib
import json
import math
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Iterator, Mapping, Sequence

from .models import (
    AdSpan,
    Change,
    Correction,
    Episode,
    Feed,
    Hit,
    SpeakerEmbedding,
    SpeakerMatch,
    SpeakerName,
    SpeakerProfile,
    Turn,
    Worker,
)

SCHEMA_VERSION = 11

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
    merged_at   TEXT,
    diarization_key TEXT,
    asr_key     TEXT,
    diar_key    TEXT,
    lease_id    TEXT,
    lease_worker TEXT,
    lease_expires TEXT
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

CREATE TABLE IF NOT EXISTS change_state (
    id             INTEGER PRIMARY KEY CHECK (id = 1),
    pruned_through INTEGER NOT NULL DEFAULT 0
);

INSERT OR IGNORE INTO change_state (id, pruned_through) VALUES (1, 0);

CREATE TABLE IF NOT EXISTS episode_tombstones (
    guid     TEXT PRIMARY KEY,
    revision INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS speaker_names (
    episode_guid TEXT NOT NULL REFERENCES episodes(guid) ON DELETE CASCADE,
    speaker      TEXT NOT NULL,
    name         TEXT NOT NULL,
    method       TEXT NOT NULL DEFAULT 'manual',
    confidence   REAL,
    updated_at   TEXT NOT NULL,
    PRIMARY KEY (episode_guid, speaker)
);

CREATE TABLE IF NOT EXISTS speaker_embeddings (
    episode_guid TEXT NOT NULL REFERENCES episodes(guid) ON DELETE CASCADE,
    speaker      TEXT NOT NULL,
    embedding    TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    PRIMARY KEY (episode_guid, speaker)
);

CREATE TABLE IF NOT EXISTS speaker_profiles (
    feed_id      INTEGER NOT NULL REFERENCES feeds(id) ON DELETE CASCADE,
    name         TEXT NOT NULL,
    embedding    TEXT NOT NULL,
    sample_count INTEGER NOT NULL,
    updated_at   TEXT NOT NULL,
    PRIMARY KEY (feed_id, name)
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

CREATE TABLE IF NOT EXISTS ad_spans (
    episode_guid TEXT NOT NULL REFERENCES episodes(guid) ON DELETE CASCADE,
    start        REAL NOT NULL CHECK (start >= 0),
    end          REAL NOT NULL CHECK (end > start),
    category     TEXT NOT NULL CHECK (category IN ('sponsor', 'self_promotion', 'affiliate', 'crowdfunding')),
    confidence   REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    evidence     TEXT NOT NULL,
    detector     TEXT NOT NULL,
    PRIMARY KEY (episode_guid, start, end, category)
);

CREATE TABLE IF NOT EXISTS ad_checks (
    episode_guid TEXT PRIMARY KEY REFERENCES episodes(guid) ON DELETE CASCADE,
    detector     TEXT NOT NULL,
    checked_at   TEXT NOT NULL,
    revision     INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_ad_spans_episode ON ad_spans(episode_guid, start, end);

-- Version 11: named remote workers, each with its own token (stored hashed).
CREATE TABLE IF NOT EXISTS workers (
    name       TEXT PRIMARY KEY,
    token_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    last_seen  TEXT
);

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


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


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
            try:
                self.conn.executescript("BEGIN IMMEDIATE;\n" + SCHEMA)
                version = self.conn.execute("PRAGMA user_version").fetchone()[0]
                if version > SCHEMA_VERSION:
                    raise RuntimeError(
                        f"database schema {version} is newer than supported schema {SCHEMA_VERSION}"
                    )
                if version < SCHEMA_VERSION:
                    self._migrate(version)
                    self.conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                self.conn.commit()
            except BaseException:
                self.conn.rollback()
                raise

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
                self.conn.execute(
                    """CREATE TABLE changes_v2 (
                        seq          INTEGER PRIMARY KEY AUTOINCREMENT,
                        episode_guid TEXT NOT NULL,
                        revision     INTEGER NOT NULL,
                        kind         TEXT NOT NULL CHECK (kind IN ('transcript', 'metadata', 'deleted')),
                        occurred_at  TEXT NOT NULL
                    )"""
                )
                self.conn.execute("INSERT INTO changes_v2 SELECT * FROM changes")
                self.conn.execute("DROP INDEX IF EXISTS idx_changes_seq")
                self.conn.execute("DROP TABLE changes")
                self.conn.execute("ALTER TABLE changes_v2 RENAME TO changes")
                self.conn.execute("CREATE INDEX idx_changes_seq ON changes(seq)")

        if version < 3:
            self.conn.execute(
                """INSERT INTO episode_tombstones (guid, revision)
                   SELECT episode_guid, MAX(revision)
                   FROM changes WHERE kind = 'deleted'
                   GROUP BY episode_guid
                   ON CONFLICT(guid) DO UPDATE SET revision = MAX(revision, excluded.revision)"""
            )

        if version < 6:
            cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(episodes)")}
            if "diarization_key" not in cols:
                self.conn.execute("ALTER TABLE episodes ADD COLUMN diarization_key TEXT")

        # Version 7 adds the local Mac/Senko voice samples. The vectors are JSON
        # so the base install can read them without NumPy.
        if version < 7:
            self.conn.execute(
                """CREATE TABLE IF NOT EXISTS speaker_embeddings (
                    episode_guid TEXT NOT NULL REFERENCES episodes(guid) ON DELETE CASCADE,
                    speaker      TEXT NOT NULL,
                    embedding    TEXT NOT NULL,
                    updated_at   TEXT NOT NULL,
                    PRIMARY KEY (episode_guid, speaker)
                )"""
            )

        if version < 8:
            self.conn.execute(
                """CREATE TABLE IF NOT EXISTS speaker_profiles (
                    feed_id      INTEGER NOT NULL REFERENCES feeds(id) ON DELETE CASCADE,
                    name         TEXT NOT NULL,
                    embedding    TEXT NOT NULL,
                    sample_count INTEGER NOT NULL,
                    updated_at   TEXT NOT NULL,
                    PRIMARY KEY (feed_id, name)
                )"""
            )

        # Version 9 adds remote-worker leases, and records which model run an
        # episode's transcript was merged from (NULL: this machine's own models).
        if version < 9:
            cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(episodes)")}
            for name in ("asr_key", "diar_key", "lease_id", "lease_worker", "lease_expires"):
                if name not in cols:
                    self.conn.execute(f"ALTER TABLE episodes ADD COLUMN {name} TEXT")

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
        """Episodes still to process, newest first. Leased episodes belong to a
        remote worker until the lease expires."""
        sql = (
            "SELECT * FROM episodes WHERE status IN (?, ?, ?) "
            "AND (lease_expires IS NULL OR lease_expires <= ?) ORDER BY published DESC, id DESC"
        )
        params: list[object] = [
            STATUS_NEW, STATUS_DOWNLOADED, STATUS_ERROR if retry_errors else STATUS_NEW, _now(),
        ]
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

    def source_guids_without_published(self, feed_id: int) -> set[str]:
        return {
            row["source_guid"]
            for row in self.conn.execute(
                "SELECT source_guid FROM episodes WHERE feed_id = ? AND published IS NULL", (feed_id,)
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

    # ---- remote work leases ----------------------------------------------

    def claim(self, worker: str, lease_seconds: int) -> tuple[Episode, str, str] | None:
        """Lease the newest downloaded episode to a worker. Returns (episode,
        lease_id, lease_expires), or None when nothing is claimable. Atomic: the
        conditional UPDATE means two claimers never get the same lease."""
        now = datetime.now(timezone.utc)
        now_text = now.isoformat(timespec="seconds")
        expires = (now + timedelta(seconds=lease_seconds)).isoformat(timespec="seconds")
        while True:
            row = self.conn.execute(
                "SELECT id FROM episodes WHERE status = ? AND audio_path IS NOT NULL "
                "AND (lease_expires IS NULL OR lease_expires <= ?) "
                "ORDER BY published DESC, id DESC LIMIT 1",
                (STATUS_DOWNLOADED, now_text),
            ).fetchone()
            if row is None:
                return None
            lease_id = secrets.token_urlsafe(16)
            with self.tx() as c:
                taken = c.execute(
                    "UPDATE episodes SET lease_id = ?, lease_worker = ?, lease_expires = ? "
                    "WHERE id = ? AND (lease_expires IS NULL OR lease_expires <= ?)",
                    (lease_id, worker, expires, row["id"], now_text),
                ).rowcount
            if taken:
                return self.episode_by_id(row["id"]), lease_id, expires

    def add_worker(self, name: str) -> str:
        """Register a worker and return its token. Only a hash is stored, so the
        token is shown this once."""
        token = secrets.token_urlsafe(32)
        try:
            with self.tx() as c:
                c.execute(
                    "INSERT INTO workers (name, token_hash, created_at) VALUES (?, ?, ?)",
                    (name, _token_hash(token), _now()),
                )
        except sqlite3.IntegrityError:
            raise ValueError(f"worker {name!r} already exists") from None
        return token

    def remove_worker(self, name: str) -> bool:
        with self.tx() as c:
            return c.execute("DELETE FROM workers WHERE name = ?", (name,)).rowcount > 0

    def seen_worker(self, token: str) -> str | None:
        """The name of the worker holding `token`, recording that it was seen."""
        digest = _token_hash(token)
        with self.tx() as c:
            row = c.execute("SELECT name FROM workers WHERE token_hash = ?", (digest,)).fetchone()
            if row:
                c.execute("UPDATE workers SET last_seen = ? WHERE token_hash = ?", (_now(), digest))
        return row["name"] if row else None

    def workers(self) -> list[Worker]:
        now = _now()
        rows = self.conn.execute(
            """SELECT w.name, w.created_at, w.last_seen,
                      (SELECT e.guid FROM episodes e WHERE e.lease_worker = w.name
                         AND e.lease_expires > ? ORDER BY e.lease_expires DESC LIMIT 1) AS lease_guid
               FROM workers w ORDER BY w.name""",
            (now,),
        ).fetchall()
        return [Worker(r["name"], r["created_at"], r["last_seen"], r["lease_guid"]) for r in rows]

    def lease_holder(self, guid: str, lease_id: str) -> Episode | None:
        """The episode if `lease_id` is its current lease. An expired lease still
        holds until someone else claims the episode."""
        row = self.conn.execute(
            "SELECT * FROM episodes WHERE guid = ? AND lease_id = ?", (guid, lease_id)
        ).fetchone()
        return _episode(row) if row else None

    def consume_lease(self, guid: str, lease_id: str) -> Episode | None:
        """End a lease, once. Returns the episode only to the one caller that
        ended it, so a retried or duplicate result is never applied twice."""
        with self.tx() as c:
            ended = c.execute(
                "UPDATE episodes SET lease_id = NULL, lease_worker = NULL, lease_expires = NULL "
                "WHERE guid = ? AND lease_id = ?",
                (guid, lease_id),
            ).rowcount
        return self.episode(guid) if ended else None

    def raw_keys(self, guid: str) -> tuple[str | None, str | None]:
        """(asr_key, diar_key) of the model run the transcript was merged from."""
        row = self.conn.execute("SELECT asr_key, diar_key FROM episodes WHERE guid = ?", (guid,)).fetchone()
        return (row["asr_key"], row["diar_key"]) if row else (None, None)

    def set_raw_keys(self, episode_id: int, asr_key: str | None, diar_key: str | None) -> None:
        with self.tx() as c:
            c.execute(
                "UPDATE episodes SET asr_key = ?, diar_key = ? WHERE id = ?", (asr_key, diar_key, episode_id)
            )

    def clear_audio(self, episode_id: int) -> None:
        """Forget the source audio path, after the file has been deleted."""
        with self.tx() as c:
            c.execute(
                "UPDATE episodes SET audio_path = NULL, updated_at = ? WHERE id = ?",
                (_now(), episode_id),
            )

    def audio_path_in_use(self, audio_path: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM episodes WHERE audio_path = ? LIMIT 1", (audio_path,)
        ).fetchone() is not None

    def speaker_names(self, episode_guid: str) -> list[SpeakerName]:
        rows = self.conn.execute(
            "SELECT episode_guid, speaker, name, method, confidence, updated_at "
            "FROM speaker_names WHERE episode_guid = ? ORDER BY speaker",
            (episode_guid,),
        )
        return [SpeakerName(**dict(row)) for row in rows]

    def speaker_embeddings(self, episode_guid: str) -> list[SpeakerEmbedding]:
        rows = self.conn.execute(
            "SELECT episode_guid, speaker, embedding, updated_at "
            "FROM speaker_embeddings WHERE episode_guid = ? ORDER BY speaker",
            (episode_guid,),
        )
        return [
            SpeakerEmbedding(
                episode_guid=row["episode_guid"],
                speaker=row["speaker"],
                embedding=tuple(json.loads(row["embedding"])),
                updated_at=row["updated_at"],
            )
            for row in rows
        ]

    def speaker_profiles(self, feed_id: int) -> list[SpeakerProfile]:
        rows = self.conn.execute(
            "SELECT feed_id, name, embedding, sample_count, updated_at "
            "FROM speaker_profiles WHERE feed_id = ? ORDER BY name",
            (feed_id,),
        )
        return [
            SpeakerProfile(
                feed_id=row["feed_id"],
                name=row["name"],
                embedding=tuple(json.loads(row["embedding"])),
                sample_count=row["sample_count"],
                updated_at=row["updated_at"],
            )
            for row in rows
        ]

    def rebuild_speaker_profiles(
        self,
        feed_id: int,
        *,
        changed_guid: str | None = None,
        notify_changes: bool = True,
    ) -> list[SpeakerProfile]:
        """Build one normalized centroid per manually named identity in a feed."""
        rows = self.conn.execute(
            """SELECT sn.name, se.embedding
               FROM speaker_names sn
               JOIN episodes e ON e.guid = sn.episode_guid
               JOIN speaker_embeddings se
                 ON se.episode_guid = sn.episode_guid AND se.speaker = sn.speaker
               WHERE e.feed_id = ? AND sn.method = 'manual'
               ORDER BY sn.name, sn.episode_guid, sn.speaker""",
            (feed_id,),
        )
        samples: dict[str, tuple[str, list[tuple[float, ...]]]] = {}
        for row in rows:
            vector = tuple(float(value) for value in json.loads(row["embedding"]))
            key = row["name"].casefold()
            if key not in samples:
                samples[key] = (row["name"], [])
            samples[key][1].append(vector)

        profiles: list[SpeakerProfile] = []
        now = _now()
        previous = {
            row["name"]: (tuple(json.loads(row["embedding"])), row["sample_count"])
            for row in self.conn.execute(
                "SELECT name, embedding, sample_count FROM speaker_profiles WHERE feed_id = ?",
                (feed_id,),
            )
        }
        current = {
            name: (centroid, len(vectors))
            for name, vectors in samples.values()
            if len(vectors) >= 2 and (centroid := _centroid(vectors)) is not None
        }
        with self.tx() as c:
            c.execute("DELETE FROM speaker_profiles WHERE feed_id = ?", (feed_id,))
            for name, (centroid, sample_count) in current.items():
                c.execute(
                    """INSERT INTO speaker_profiles
                       (feed_id, name, embedding, sample_count, updated_at)
                       VALUES (?, ?, ?, ?, ?)""",
                    (feed_id, name, json.dumps(centroid), sample_count, now),
                )
                profiles.append(SpeakerProfile(feed_id, name, centroid, sample_count, now))

            if notify_changes and previous != current:
                self._invalidate_feed_metadata(c, feed_id, changed_guid, now)
        return profiles

    def invalidate_feed_metadata(self, feed_id: int, *, changed_guid: str | None = None) -> None:
        """Notify change-feed consumers that shared speaker labels changed."""
        now = _now()
        with self.tx() as c:
            self._invalidate_feed_metadata(c, feed_id, changed_guid, now)

    def _invalidate_feed_metadata(
        self,
        c: sqlite3.Connection,
        feed_id: int,
        changed_guid: str | None,
        now: str,
    ) -> None:
        for row in c.execute(
            "SELECT id, guid, revision FROM episodes WHERE feed_id = ?", (feed_id,)
        ).fetchall():
            if row["guid"] == changed_guid:
                continue
            c.execute("UPDATE episodes SET updated_at = ? WHERE id = ?", (now, row["id"]))
            self._insert_change(c, row["guid"], row["revision"], "metadata")

    def most_common_manual_speaker_name(self, feed_id: int) -> str | None:
        """Return a recurring manual label when a profile table has no entry yet."""
        row = self.conn.execute(
            """SELECT sn.name, COUNT(DISTINCT sn.episode_guid) AS samples
               FROM speaker_names sn
               JOIN episodes e ON e.guid = sn.episode_guid
               WHERE e.feed_id = ? AND sn.method = 'manual'
               GROUP BY sn.name COLLATE NOCASE
               HAVING COUNT(DISTINCT sn.episode_guid) >= 2
               ORDER BY samples DESC, sn.name COLLATE NOCASE
               LIMIT 1""",
            (feed_id,),
        ).fetchone()
        return row["name"] if row else None

    def speaker_matches(self, episode_guid: str, min_score: float = 0.70) -> list[SpeakerMatch]:
        episode = self.conn.execute(
            "SELECT feed_id FROM episodes WHERE guid = ?", (episode_guid,)
        ).fetchone()
        if episode is None:
            raise LookupError(f"no episode {episode_guid}")

        targets = self.speaker_embeddings(episode_guid)
        stored_profiles = self.speaker_profiles(episode["feed_id"])
        if stored_profiles:
            profiles = [
                (profile.name, profile.embedding, profile.sample_count)
                for profile in stored_profiles
            ]
        else:
            rows = self.conn.execute(
                """SELECT sn.name, se.embedding
                   FROM speaker_names sn
                   JOIN episodes e ON e.guid = sn.episode_guid
                   JOIN speaker_embeddings se
                     ON se.episode_guid = sn.episode_guid AND se.speaker = sn.speaker
                   WHERE e.feed_id = ? AND e.guid != ? AND sn.method = 'manual'""",
                (episode["feed_id"], episode_guid),
            )
            samples: dict[str, list[tuple[float, ...]]] = {}
            for row in rows:
                samples.setdefault(row["name"], []).append(tuple(json.loads(row["embedding"])))
            profiles = [
                (name, centroid, len(samples[name]))
                for name, vectors in samples.items()
                if (centroid := _centroid(vectors)) is not None
            ]

        matches = []
        for target in targets:
            candidates = [
                (name, _cosine(target.embedding, embedding), sample_count)
                for name, embedding, sample_count in profiles
            ]
            if not candidates:
                continue
            name, score, sample_count = max(candidates, key=lambda item: item[1])
            if score >= min_score:
                matches.append(
                    SpeakerMatch(episode_guid, target.speaker, name, score, sample_count)
                )
        return matches

    def set_speaker_name(
        self,
        episode_guid: str,
        speaker: str,
        name: str,
        *,
        method: str = "manual",
        confidence: float | None = None,
    ) -> SpeakerName:
        return self._set_speaker_name(
            episode_guid, speaker, name, method=method, confidence=confidence
        )[0]

    def _set_speaker_name(
        self,
        episode_guid: str,
        speaker: str,
        name: str,
        *,
        method: str = "manual",
        confidence: float | None = None,
    ) -> tuple[SpeakerName, bool]:
        with self.tx() as c:
            episode = c.execute(
                "SELECT id, revision FROM episodes WHERE guid = ?", (episode_guid,)
            ).fetchone()
            if episode is None:
                raise LookupError(f"no episode {episode_guid}")
            if c.execute(
                "SELECT 1 FROM turns WHERE episode_id = ? AND speaker = ? LIMIT 1",
                (episode["id"], speaker),
            ).fetchone() is None:
                raise ValueError(f"unknown speaker {speaker}")
            existing = c.execute(
                "SELECT name, method, confidence FROM speaker_names "
                "WHERE episode_guid = ? AND speaker = ?",
                (episode_guid, speaker),
            ).fetchone()
            changed = existing is None or tuple(existing) != (name, method, confidence)
            if changed:
                now = _now()
                c.execute(
                    """INSERT INTO speaker_names
                       (episode_guid, speaker, name, method, confidence, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?)
                       ON CONFLICT(episode_guid, speaker) DO UPDATE SET
                         name = excluded.name,
                         method = excluded.method,
                         confidence = excluded.confidence,
                         updated_at = excluded.updated_at""",
                    (episode_guid, speaker, name, method, confidence, now),
                )
                c.execute(
                    "UPDATE episodes SET updated_at = ? WHERE id = ?",
                    (now, episode["id"]),
                )
                self._insert_change(c, episode_guid, episode["revision"], "metadata")
            row = c.execute(
                "SELECT episode_guid, speaker, name, method, confidence, updated_at "
                "FROM speaker_names WHERE episode_guid = ? AND speaker = ?",
                (episode_guid, speaker),
            ).fetchone()
            if row is None:
                raise LookupError(f"no speaker name for {speaker}")
            return SpeakerName(**dict(row)), changed

    def delete_speaker_name(self, episode_guid: str, speaker: str) -> bool:
        with self.tx() as c:
            episode = c.execute(
                "SELECT revision FROM episodes WHERE guid = ?", (episode_guid,)
            ).fetchone()
            if episode is None:
                raise LookupError(f"no episode {episode_guid}")
            deleted = c.execute(
                "DELETE FROM speaker_names WHERE episode_guid = ? AND speaker = ?",
                (episode_guid, speaker),
            ).rowcount
            if deleted:
                now = _now()
                c.execute(
                    "UPDATE episodes SET updated_at = ? WHERE guid = ?",
                    (now, episode_guid),
                )
                self._insert_change(c, episode_guid, episode["revision"], "metadata")
        return bool(deleted)

    # ---- transcripts -----------------------------------------------------

    def replace_turns(
        self,
        episode_id: int,
        turns: Sequence[Turn],
        corrections: Sequence[Correction] = (),
        *,
        diarization_key: str | None = None,
        speaker_embeddings: Mapping[str, Sequence[float]] | None = None,
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
                "SELECT guid, revision, diarization_key FROM episodes WHERE id = ?",
                (episode_id,),
            ).fetchone()
            if episode is None:
                raise LookupError(f"no episode {episode_id}")
            if diarization_key is not None and (
                episode["diarization_key"] is not None
                and episode["diarization_key"] != diarization_key
            ):
                c.execute(
                    "DELETE FROM speaker_names WHERE episode_guid = ?",
                    (episode["guid"],),
                )
            if diarization_key is not None:
                c.execute(
                    "UPDATE episodes SET diarization_key = ? WHERE id = ?",
                    (diarization_key, episode_id),
                )
            speakers = {turn.speaker for turn in turns}
            if speakers:
                placeholders = ", ".join("?" for _ in speakers)
                c.execute(
                    f"DELETE FROM speaker_names WHERE episode_guid = ? "
                    f"AND speaker NOT IN ({placeholders})",
                    [episode["guid"], *speakers],
                )
            else:
                c.execute(
                    "DELETE FROM speaker_names WHERE episode_guid = ?",
                    (episode["guid"],),
                )
            if speaker_embeddings is not None:
                now = _now()
                c.execute(
                    "DELETE FROM speaker_embeddings WHERE episode_guid = ?",
                    (episode["guid"],),
                )
                c.executemany(
                    "INSERT INTO speaker_embeddings "
                    "(episode_guid, speaker, embedding, updated_at) VALUES (?, ?, ?, ?)",
                    [
                        (episode["guid"], speaker, json.dumps([float(x) for x in embedding]), now)
                        for speaker, embedding in speaker_embeddings.items()
                    ],
                )
            c.execute("DELETE FROM ad_spans WHERE episode_guid = ?", (episode["guid"],))
            c.execute("DELETE FROM ad_checks WHERE episode_guid = ?", (episode["guid"],))
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
        row = self.conn.execute(
            """SELECT MAX(
                       COALESCE((SELECT MAX(seq) FROM changes), 0),
                       COALESCE((SELECT seq FROM sqlite_sequence WHERE name = 'changes'), 0)
                   ) AS seq"""
        ).fetchone()
        return int(row["seq"])

    def changes(self, after: int = 0, limit: int = 100) -> list[Change]:
        rows = self.conn.execute(
            """SELECT seq, episode_guid, revision, kind, occurred_at
               FROM changes WHERE seq > ? ORDER BY seq LIMIT ?""",
            (after, limit),
        )
        return [Change(**dict(row)) for row in rows]

    def prune_changes(self, through: int) -> int:
        """Delete change events through a caller-confirmed sequence number."""
        if through < 0:
            raise ValueError("change sequence must be non-negative")
        with self.tx() as c:
            c.execute(
                "UPDATE change_state SET pruned_through = MAX(pruned_through, ?)",
                (through,),
            )
            return c.execute("DELETE FROM changes WHERE seq <= ?", (through,)).rowcount

    def pruned_through(self) -> int:
        row = self.conn.execute(
            "SELECT pruned_through FROM change_state WHERE id = 1"
        ).fetchone()
        return int(row["pruned_through"])

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

    def ad_spans(self, episode_guid: str) -> list[AdSpan]:
        rows = self.conn.execute(
            """SELECT episode_guid, start, end, category, confidence, evidence, detector
               FROM ad_spans WHERE episode_guid = ? ORDER BY start, end""",
            (episode_guid,),
        )
        return [AdSpan(**dict(row)) for row in rows]

    def ads_checked(self, episode_guid: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM ad_checks WHERE episode_guid = ?", (episode_guid,)
        ).fetchone() is not None

    def replace_ad_spans(
        self,
        episode_guid: str,
        spans: Sequence[AdSpan],
        detector: str,
        *,
        expected_revision: int,
    ) -> bool:
        """Store one classification only if the transcript revision still matches.

        Returns False when a transcript rewrite raced the model request. A newly
        completed classification emits a transcript change even when it found no
        ads, so index consumers can rebuild from the clean transcript view.
        """
        ordered = sorted(spans, key=lambda span: (span.start, span.end, span.category))
        with self.tx() as c:
            episode = c.execute(
                "SELECT revision FROM episodes WHERE guid = ?", (episode_guid,)
            ).fetchone()
            if episode is None:
                raise LookupError(f"no episode {episode_guid}")
            if episode["revision"] != expected_revision:
                return False

            previous = [
                tuple(row)
                for row in c.execute(
                    """SELECT start, end, category, confidence, evidence, detector
                       FROM ad_spans WHERE episode_guid = ? ORDER BY start, end, category""",
                    (episode_guid,),
                )
            ]
            current = [
                (span.start, span.end, span.category, span.confidence, span.evidence, span.detector)
                for span in ordered
            ]
            review = c.execute(
                "SELECT detector FROM ad_checks WHERE episode_guid = ?", (episode_guid,)
            ).fetchone()
            changed = previous != current or review is None or review["detector"] != detector
            now = _now()
            if changed:
                c.execute("DELETE FROM ad_spans WHERE episode_guid = ?", (episode_guid,))
                c.executemany(
                    """INSERT INTO ad_spans
                       (episode_guid, start, end, category, confidence, evidence, detector)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    [
                        (episode_guid, span.start, span.end, span.category,
                         span.confidence, span.evidence, span.detector)
                        for span in ordered
                    ],
                )
                revision = expected_revision + 1
                c.execute(
                    "UPDATE episodes SET revision = ?, updated_at = ? WHERE guid = ?",
                    (revision, now, episode_guid),
                )
                self._insert_change(c, episode_guid, revision, "transcript")
            else:
                revision = expected_revision
            c.execute(
                """INSERT INTO ad_checks (episode_guid, detector, checked_at, revision)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(episode_guid) DO UPDATE SET
                     detector = excluded.detector,
                     checked_at = excluded.checked_at,
                     revision = excluded.revision""",
                (episode_guid, detector, now, revision),
            )
            return True

    def corrections(self, episode_id: int) -> list[Correction]:
        rows = self.conn.execute(
            "SELECT heard, fixed, count FROM corrections WHERE episode_id = ? ORDER BY count DESC, heard",
            (episode_id,),
        )
        return [Correction(**dict(r)) for r in rows]

    def search(self, query: str, limit: int = 20, *, include_ads: bool = False) -> list[Hit]:
        try:
            return self._search(query, limit, include_ads=include_ads)
        except sqlite3.OperationalError:
            # Not valid FTS5 syntax (e.g. "don't", "a-b"): retry as a literal phrase.
            return self._search('"' + query.replace('"', '""') + '"', limit, include_ads=include_ads)

    def _search(self, query: str, limit: int, *, include_ads: bool = False) -> list[Hit]:
        ad_filter = "" if include_ads else """
                 AND NOT EXISTS (
                     SELECT 1 FROM ad_spans a
                     WHERE a.episode_guid = e.guid AND a.start < t.end AND a.end > t.start
                 )"""
        rows = self.conn.execute(
            f"""SELECT t.episode_id, e.guid, e.title AS episode_title, t.speaker, t.start, t.end,
                      snippet(turns_fts, 0, '[', ']', ' … ', 12) AS snippet
               FROM turns_fts f
               JOIN turns t     ON t.id = f.turn_id
               JOIN episodes e  ON e.id = t.episode_id
               WHERE turns_fts MATCH ?
                 {ad_filter}
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


def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != len(right):
        return -1.0
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if not left_norm or not right_norm:
        return -1.0
    return sum(a * b for a, b in zip(left, right)) / (left_norm * right_norm)


def _centroid(vectors: Sequence[Sequence[float]]) -> tuple[float, ...] | None:
    if not vectors:
        return None
    size = len(vectors[0])
    if not size or any(len(vector) != size for vector in vectors):
        return None
    mean = [sum(vector[index] for vector in vectors) / len(vectors) for index in range(size)]
    norm = math.sqrt(sum(value * value for value in mean))
    if not norm:
        return None
    return tuple(value / norm for value in mean)
