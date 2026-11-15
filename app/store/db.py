"""SQLite 连接与建表。

单用户桌面工具，用同步 sqlite3 + 一把锁即可，不引 ORM 也不异步化。
"""
from __future__ import annotations

import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from app.paths import DB_PATH

SCHEMA_VERSION = 2

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None

_SCHEMA = """
CREATE TABLE IF NOT EXISTS songs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source      TEXT    NOT NULL,
    source_id   TEXT    NOT NULL,
    extra       TEXT    NOT NULL DEFAULT '',
    title       TEXT    NOT NULL,
    artists     TEXT    NOT NULL DEFAULT '',
    album       TEXT    NOT NULL DEFAULT '',
    cover_url   TEXT    NOT NULL DEFAULT '',
    duration    INTEGER NOT NULL DEFAULT 0,
    quality     TEXT    NOT NULL DEFAULT '',
    created_at  INTEGER NOT NULL,
    UNIQUE(source, source_id, extra)
);
CREATE INDEX IF NOT EXISTS idx_songs_lookup ON songs(source, source_id);

CREATE TABLE IF NOT EXISTS playlists (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT    NOT NULL,
    kind         TEXT    NOT NULL DEFAULT 'normal',
    sort_order   INTEGER NOT NULL DEFAULT 0,
    created_at   INTEGER NOT NULL,
    remote_type  TEXT    NOT NULL DEFAULT '',
    remote_id    TEXT    NOT NULL DEFAULT '',
    synced_at    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS playlist_items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    playlist_id INTEGER NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
    song_id     INTEGER NOT NULL REFERENCES songs(id)     ON DELETE CASCADE,
    sort_order  INTEGER NOT NULL DEFAULT 0,
    origin      TEXT    NOT NULL DEFAULT 'manual',
    UNIQUE(playlist_id, song_id)
);
CREATE INDEX IF NOT EXISTS idx_items_playlist ON playlist_items(playlist_id, sort_order);

CREATE TABLE IF NOT EXISTS queue (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    song_id       INTEGER NOT NULL REFERENCES songs(id) ON DELETE CASCADE,
    requester     TEXT    NOT NULL DEFAULT '',
    requester_uid TEXT    NOT NULL DEFAULT '',
    priority      INTEGER NOT NULL DEFAULT 0,
    status        TEXT    NOT NULL DEFAULT 'pending',
    created_at    INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_queue_status ON queue(status, priority DESC, id);

CREATE TABLE IF NOT EXISTS history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    song_id     INTEGER NOT NULL,
    requester   TEXT    NOT NULL DEFAULT '',
    played_at   INTEGER NOT NULL,
    playlist_id INTEGER NOT NULL DEFAULT 0,
    source      TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_history_time ON history(played_at DESC);

CREATE TABLE IF NOT EXISTS auth (
    provider   TEXT PRIMARY KEY,
    cookie     TEXT NOT NULL DEFAULT '',
    account    TEXT NOT NULL DEFAULT '',
    uid        TEXT NOT NULL DEFAULT '',
    updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
);
"""


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


# 旧库升级要补的列：(表, 列, 定义)。CREATE TABLE IF NOT EXISTS 不会给已存在的表加列。
_MIGRATIONS = [
    ("playlists", "remote_type", "TEXT NOT NULL DEFAULT ''"),
    ("playlists", "remote_id", "TEXT NOT NULL DEFAULT ''"),
    ("playlists", "synced_at", "INTEGER NOT NULL DEFAULT 0"),
    ("playlist_items", "origin", "TEXT NOT NULL DEFAULT 'manual'"),
]


def _migrate(conn: sqlite3.Connection) -> None:
    for table, column, decl in _MIGRATIONS:
        cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def init_db() -> None:
    global _conn
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _lock:
        if _conn is None:
            _conn = _connect()
        _conn.executescript(_SCHEMA)
        _migrate(_conn)
        version = _conn.execute("PRAGMA user_version").fetchone()[0]
        if version != SCHEMA_VERSION:
            _conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        _conn.commit()


def close_db() -> None:
    global _conn
    with _lock:
        if _conn is not None:
            _conn.close()
            _conn = None


def get_conn() -> sqlite3.Connection:
    if _conn is None:
        init_db()
    assert _conn is not None
    return _conn


@contextmanager
def tx() -> Iterator[sqlite3.Connection]:
    """事务上下文，异常自动回滚。"""
    conn = get_conn()
    with _lock:
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def now() -> int:
    return int(time.time())


def kv_get(key: str, default: str = "") -> str:
    row = get_conn().execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def kv_set(key: str, value: str) -> None:
    with tx() as conn:
        conn.execute(
            "INSERT INTO kv(key, value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
