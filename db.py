"""Local SQLite store for lyricswidget: a persistent lyrics cache.

Why SQLite: it's in the Python standard library (no dependency, no server), stores to a
single file next to the app, is transactional, and handles access from the widget's worker
threads and the probe subprocess safely. It persists the lyrics cache across restarts so
LRCLIB is queried at most once per track ever (not once per run), cutting API traffic.

The cache stores both hits and confirmed misses. A miss is kept only for MISS_TTL so that
a track LRCLIB didn't have yet can be re-checked later (its catalogue grows over time).
"""
from __future__ import annotations

import sqlite3
import threading
import time

from config import DB_FILENAME, MISS_TTL_SECONDS
from logging_setup import get_logger

log = get_logger("db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS lyrics_cache (
    artist      TEXT NOT NULL,
    title       TEXT NOT NULL,
    synced      TEXT NOT NULL DEFAULT '',
    plain       TEXT NOT NULL DEFAULT '',
    found       INTEGER NOT NULL,          -- 1 = lyrics present, 0 = confirmed miss
    fetched_at  REAL NOT NULL,             -- unix epoch seconds
    PRIMARY KEY (artist, title)
);
"""

# One connection per thread (sqlite3 connections aren't safe to share across threads).
_local = threading.local()
_init_lock = threading.Lock()
_initialized = False


def _db_path() -> str:
    import os
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), DB_FILENAME)


def _connect() -> sqlite3.Connection:
    """Return this thread's connection, creating and initializing the DB on first use."""
    global _initialized
    conn = getattr(_local, "conn", None)
    if conn is not None:
        return conn

    path = _db_path()
    conn = sqlite3.connect(path, timeout=5.0)
    conn.row_factory = sqlite3.Row
    # WAL improves concurrent read/write between the widget threads and any other reader.
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")

    with _init_lock:
        if not _initialized:
            conn.executescript(_SCHEMA)
            conn.commit()
            _initialized = True
            log.info("SQLite cache ready at %s", path)

    _local.conn = conn
    return conn


def _key(artist: str, title: str) -> tuple[str, str]:
    """Normalise an (artist, title) pair to the stored, case-folded key."""
    return (artist.casefold().strip(), title.casefold().strip())


def get_cached(artist: str, title: str) -> tuple[bool, dict | None]:
    """Look up a track in the cache.

    Returns (hit, payload):
      - (False, None): not cached (or an expired miss) -> caller should query LRCLIB.
      - (True, None):  cached confirmed miss, still fresh -> caller should NOT query.
      - (True, {...}): cached lyrics -> use directly.
    """
    a, t = _key(artist, title)
    try:
        row = _connect().execute(
            "SELECT synced, plain, found, fetched_at FROM lyrics_cache WHERE artist=? AND title=?",
            (a, t),
        ).fetchone()
    except sqlite3.Error:
        log.exception("get_cached: DB read failed")
        return (False, None)

    if row is None:
        return (False, None)

    if not row["found"]:
        # A confirmed miss: honour it only until it expires, then allow a re-check.
        if time.time() - row["fetched_at"] < MISS_TTL_SECONDS:
            return (True, None)
        log.debug("get_cached: stale miss for %r/%r, will re-check", a, t)
        return (False, None)

    return (True, {"synced": row["synced"], "plain": row["plain"]})


def put_cached(artist: str, title: str, synced: str, plain: str, found: bool) -> None:
    """Insert or replace a cache entry (hit or confirmed miss)."""
    a, t = _key(artist, title)
    try:
        conn = _connect()
        conn.execute(
            "INSERT OR REPLACE INTO lyrics_cache (artist, title, synced, plain, found, fetched_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (a, t, synced or "", plain or "", 1 if found else 0, time.time()),
        )
        conn.commit()
    except sqlite3.Error:
        log.exception("put_cached: DB write failed")
