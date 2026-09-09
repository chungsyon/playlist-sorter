"""SQLite persistence.

Two jobs here:
  1. Cache anything expensive (track metadata, Last.fm tags, classifications)
     so a re-run costs zero model calls.
  2. Hold job state so a run that dies at song 3,400 resumes at 3,400.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

from .config import settings
from .models import Assignment, Track

_local = threading.local()

SCHEMA = """
CREATE TABLE IF NOT EXISTS tracks (
    video_id         TEXT PRIMARY KEY,
    title            TEXT NOT NULL,
    artists          TEXT DEFAULT '',
    album            TEXT DEFAULT '',
    duration_seconds INTEGER DEFAULT 0,
    is_available     INTEGER DEFAULT 1,
    fetched_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tags (
    video_id   TEXT PRIMARY KEY,
    tags       TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);

-- Keyed by bucket_hash so editing a bucket description correctly invalidates.
CREATE TABLE IF NOT EXISTS classifications (
    video_id    TEXT NOT NULL,
    bucket_hash TEXT NOT NULL,
    playlists   TEXT NOT NULL,
    confidence  REAL DEFAULT 0,
    reason      TEXT DEFAULT '',
    provider    TEXT DEFAULT '',
    created_at  TEXT NOT NULL,
    PRIMARY KEY (video_id, bucket_hash)
);

CREATE TABLE IF NOT EXISTS jobs (
    id                 TEXT PRIMARY KEY,
    source_playlist_id TEXT NOT NULL,
    source_name        TEXT DEFAULT '',
    buckets            TEXT NOT NULL,
    bucket_hash        TEXT NOT NULL,
    status             TEXT NOT NULL,
    total              INTEGER DEFAULT 0,
    classified         INTEGER DEFAULT 0,
    provider_tallies   TEXT DEFAULT '{}',
    error              TEXT DEFAULT '',
    track_ids          TEXT DEFAULT '[]',
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL
);

-- User edits from the review screen. Always win over the model's answer.
CREATE TABLE IF NOT EXISTS overrides (
    job_id    TEXT NOT NULL,
    video_id  TEXT NOT NULL,
    playlists TEXT NOT NULL,
    PRIMARY KEY (job_id, video_id)
);

-- What we actually wrote to YouTube Music, so Apply is safely re-runnable.
CREATE TABLE IF NOT EXISTS applied (
    job_id      TEXT NOT NULL,
    playlist_id TEXT NOT NULL,
    video_id    TEXT NOT NULL,
    applied_at  TEXT NOT NULL,
    PRIMARY KEY (job_id, playlist_id, video_id)
);

CREATE INDEX IF NOT EXISTS idx_class_hash ON classifications(bucket_hash);
CREATE INDEX IF NOT EXISTS idx_applied_job ON applied(job_id);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(settings.database_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def get_conn() -> sqlite3.Connection:
    """One connection per thread — the job runner and the request handlers
    touch the DB concurrently."""
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = _connect()
        _local.conn = conn
    return conn


@contextmanager
def tx() -> Iterator[sqlite3.Connection]:
    conn = get_conn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def init() -> None:
    with tx() as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)


def _migrate(conn: sqlite3.Connection) -> None:
    """Additive column migrations for databases created by an earlier version."""
    existing = {r["name"] for r in conn.execute("PRAGMA table_info(jobs)")}
    for column, ddl in (("track_ids", "TEXT DEFAULT '[]'"),):
        if column not in existing:
            conn.execute(f"ALTER TABLE jobs ADD COLUMN {column} {ddl}")


# --------------------------------------------------------------------------
# tracks
# --------------------------------------------------------------------------

def upsert_tracks(tracks: list[Track]) -> None:
    if not tracks:
        return
    now = _now()
    with tx() as conn:
        conn.executemany(
            """INSERT INTO tracks
                 (video_id, title, artists, album, duration_seconds, is_available, fetched_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(video_id) DO UPDATE SET
                 title=excluded.title, artists=excluded.artists, album=excluded.album,
                 duration_seconds=excluded.duration_seconds,
                 is_available=excluded.is_available, fetched_at=excluded.fetched_at""",
            [
                (t.video_id, t.title, t.artists, t.album,
                 t.duration_seconds, int(t.is_available), now)
                for t in tracks
            ],
        )


def get_tracks(video_ids: list[str]) -> dict[str, Track]:
    if not video_ids:
        return {}
    out: dict[str, Track] = {}
    conn = get_conn()
    for chunk in _chunks(video_ids, 900):  # stay under SQLite's variable limit
        marks = ",".join("?" * len(chunk))
        rows = conn.execute(
            f"SELECT * FROM tracks WHERE video_id IN ({marks})", chunk
        ).fetchall()
        for r in rows:
            out[r["video_id"]] = Track(
                video_id=r["video_id"], title=r["title"], artists=r["artists"],
                album=r["album"], duration_seconds=r["duration_seconds"],
                is_available=bool(r["is_available"]),
            )
    return out


# --------------------------------------------------------------------------
# tags
# --------------------------------------------------------------------------

def get_cached_tags(video_ids: list[str]) -> dict[str, list[str]]:
    if not video_ids:
        return {}
    out: dict[str, list[str]] = {}
    conn = get_conn()
    for chunk in _chunks(video_ids, 900):
        marks = ",".join("?" * len(chunk))
        rows = conn.execute(
            f"SELECT video_id, tags FROM tags WHERE video_id IN ({marks})", chunk
        ).fetchall()
        for r in rows:
            out[r["video_id"]] = json.loads(r["tags"])
    return out


def save_tags(mapping: dict[str, list[str]]) -> None:
    if not mapping:
        return
    now = _now()
    with tx() as conn:
        conn.executemany(
            """INSERT INTO tags (video_id, tags, fetched_at) VALUES (?, ?, ?)
               ON CONFLICT(video_id) DO UPDATE SET
                 tags=excluded.tags, fetched_at=excluded.fetched_at""",
            [(vid, json.dumps(tags), now) for vid, tags in mapping.items()],
        )


# --------------------------------------------------------------------------
# classifications
# --------------------------------------------------------------------------

def get_classifications(bucket_hash: str, video_ids: list[str]) -> dict[str, Assignment]:
    if not video_ids:
        return {}
    out: dict[str, Assignment] = {}
    conn = get_conn()
    for chunk in _chunks(video_ids, 900):
        marks = ",".join("?" * len(chunk))
        rows = conn.execute(
            f"""SELECT * FROM classifications
                WHERE bucket_hash = ? AND video_id IN ({marks})""",
            [bucket_hash, *chunk],
        ).fetchall()
        for r in rows:
            out[r["video_id"]] = Assignment(
                video_id=r["video_id"], playlists=json.loads(r["playlists"]),
                confidence=r["confidence"], reason=r["reason"], provider=r["provider"],
            )
    return out


def save_classifications(bucket_hash: str, assignments: list[Assignment]) -> None:
    if not assignments:
        return
    now = _now()
    with tx() as conn:
        conn.executemany(
            """INSERT INTO classifications
                 (video_id, bucket_hash, playlists, confidence, reason, provider, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(video_id, bucket_hash) DO UPDATE SET
                 playlists=excluded.playlists, confidence=excluded.confidence,
                 reason=excluded.reason, provider=excluded.provider,
                 created_at=excluded.created_at""",
            [
                (a.video_id, bucket_hash, json.dumps(a.playlists),
                 a.confidence, a.reason, a.provider, now)
                for a in assignments
            ],
        )


# --------------------------------------------------------------------------
# jobs
# --------------------------------------------------------------------------

def create_job(job_id: str, source_playlist_id: str, source_name: str,
               buckets_json: str, bucket_hash_: str, total: int) -> None:
    now = _now()
    with tx() as conn:
        conn.execute(
            """INSERT INTO jobs (id, source_playlist_id, source_name, buckets,
                                 bucket_hash, status, total, classified,
                                 provider_tallies, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, 'pending', ?, 0, '{}', ?, ?)""",
            (job_id, source_playlist_id, source_name, buckets_json,
             bucket_hash_, total, now, now),
        )


def update_job(job_id: str, **fields: Any) -> None:
    if not fields:
        return
    fields["updated_at"] = _now()
    sets = ", ".join(f"{k} = ?" for k in fields)
    with tx() as conn:
        conn.execute(f"UPDATE jobs SET {sets} WHERE id = ?",
                     [*fields.values(), job_id])


def get_job(job_id: str) -> dict[str, Any] | None:
    row = get_conn().execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return dict(row) if row else None


def list_jobs(limit: int = 25) -> list[dict[str, Any]]:
    rows = get_conn().execute(
        "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]


# --------------------------------------------------------------------------
# overrides + applied
# --------------------------------------------------------------------------

def save_override(job_id: str, video_id: str, playlists: list[str]) -> None:
    with tx() as conn:
        conn.execute(
            """INSERT INTO overrides (job_id, video_id, playlists) VALUES (?, ?, ?)
               ON CONFLICT(job_id, video_id) DO UPDATE SET playlists=excluded.playlists""",
            (job_id, video_id, json.dumps(playlists)),
        )


def get_overrides(job_id: str) -> dict[str, list[str]]:
    rows = get_conn().execute(
        "SELECT video_id, playlists FROM overrides WHERE job_id = ?", (job_id,)
    ).fetchall()
    return {r["video_id"]: json.loads(r["playlists"]) for r in rows}


def mark_applied(job_id: str, playlist_id: str, video_ids: list[str]) -> None:
    if not video_ids:
        return
    now = _now()
    with tx() as conn:
        conn.executemany(
            """INSERT OR IGNORE INTO applied (job_id, playlist_id, video_id, applied_at)
               VALUES (?, ?, ?, ?)""",
            [(job_id, playlist_id, vid, now) for vid in video_ids],
        )


def get_applied(job_id: str) -> set[tuple[str, str]]:
    rows = get_conn().execute(
        "SELECT playlist_id, video_id FROM applied WHERE job_id = ?", (job_id,)
    ).fetchall()
    return {(r["playlist_id"], r["video_id"]) for r in rows}


def _chunks(items: list[str], size: int) -> Iterator[list[str]]:
    for i in range(0, len(items), size):
        yield items[i:i + size]
