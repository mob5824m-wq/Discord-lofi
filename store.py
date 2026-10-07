"""SQLite access shared by :mod:`bot`, :mod:`player` and :mod:`dashboard`.

The database holds what the bot learns while running - play history, listener
counts, activity events - as opposed to :file:`config.json`, which holds what
the operator decided. Splitting them matters: config is small, hand-editable
and survives a database reset, while history grows forever and must survive a
config rewrite.

The helpers are deliberately tiny and mirror the ones the rest of the code
already uses: one connection per statement, opened with ``closing(...)`` so it
is always released, rows returned as :class:`sqlite3.Row`. Voice playback runs
on the same event loop, and a long-lived connection held open across an
``await`` is a recipe for "database is locked" when the dashboard writes while
a track ends.

The path is read from :mod:`paths` on every call rather than captured at import
time, so a test that redirects ``LOFI_HOME`` before importing is honoured.
"""

from __future__ import annotations

import os
import sqlite3
import time
from contextlib import closing
from typing import Any, Optional

import paths


SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS play_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    station_id TEXT NOT NULL DEFAULT '',
    station_name TEXT NOT NULL DEFAULT '',
    track_title TEXT NOT NULL DEFAULT '',
    channel_id INTEGER,
    requested_by INTEGER,
    started_at REAL NOT NULL,
    ended_at REAL,
    ended_reason TEXT,
    peak_listeners INTEGER NOT NULL DEFAULT 0,
    seconds_played REAL NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS play_sessions_guild_started
    ON play_sessions (guild_id, started_at DESC);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER,
    kind TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    actor_id INTEGER,
    created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS events_guild_created
    ON events (guild_id, created_at DESC);

CREATE TABLE IF NOT EXISTS track_plays (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    station_id TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL,
    played_at REAL NOT NULL,
    listeners INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS track_plays_title ON track_plays (title, played_at DESC);
"""


def db_path() -> str:
    """The SQLite file every helper here writes to.

    Read from :mod:`paths` on every call rather than captured at import time, so
    ``LOFI_DB`` (and a test that redirects ``LOFI_HOME``) is honoured.
    """
    override = os.environ.get("LOFI_DB", "").strip()
    return override or paths.DB_PATH


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(db_path(), timeout=10)
    conn.row_factory = sqlite3.Row
    # WAL keeps readers (the dashboard polling history) from blocking the
    # writer (a track ending mid-request).
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db() -> None:
    """Create the tables if they are missing. Safe to call on every start."""
    with closing(_connect()) as conn:
        conn.executescript(SCHEMA)
        row = conn.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
        if row is None:
            conn.execute("INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
        conn.commit()


def _write(query: str, params: tuple = ()) -> int:
    """Run one write, recreating the schema once if a table has gone missing.

    The tables are created at start-up, but the database file can be deleted or
    replaced underneath a long-running bot, and a dashboard request should not
    fail with "no such table" when the fix is one ``executescript`` away.
    """
    global _SCHEMA_READY
    try:
        return _write_once(query, params)
    except sqlite3.OperationalError as exc:
        if "no such table" not in str(exc).lower() or not _SCHEMA_READY:
            raise
        _SCHEMA_READY = False
        init_db()
        _SCHEMA_READY = True
        return _write_once(query, params)


def _write_once(query: str, params: tuple = ()) -> int:
    with closing(_connect()) as conn:
        cursor = conn.execute(query, params)
        conn.commit()
        return int(cursor.lastrowid or 0)


_SCHEMA_READY = True


def db_execute(query: str, params: tuple = ()) -> None:
    """Run one statement and commit it."""
    _write(query, params)


def db_insert(query: str, params: tuple = ()) -> int:
    """Run one INSERT and return the new row id (``lastrowid``)."""
    return _write(query, params)


def db_fetchall(query: str, params: tuple = ()) -> list[sqlite3.Row]:
    """Run one query and return every row."""
    with closing(_connect()) as conn:
        return list(conn.execute(query, params).fetchall())


def db_fetchone(query: str, params: tuple = ()) -> Optional[sqlite3.Row]:
    """Run one query and return the first row (or ``None``)."""
    with closing(_connect()) as conn:
        return conn.execute(query, params).fetchone()


def _fetch(query: str, params: tuple = ()) -> list[sqlite3.Row]:
    """Read with the same self-healing behaviour as the writers."""
    global _SCHEMA_READY
    try:
        return db_fetchall(query, params)
    except sqlite3.OperationalError as exc:
        if "no such table" not in str(exc).lower():
            raise
        init_db()
        _SCHEMA_READY = True
        return db_fetchall(query, params)


# --------------------------------------------------------------------------- #
# Play history
# --------------------------------------------------------------------------- #
def start_session(
    guild_id: int,
    *,
    station_id: str,
    station_name: str,
    track_title: str,
    channel_id: Optional[int],
    requested_by: Optional[int],
) -> int:
    """Open a play-session row and return its id.

    A session is one stretch of audio in one guild: it starts when the bot
    begins playing and ends when it stops, skips away, or is disconnected. The
    dashboard's history reads these rows, which is why "what was playing at 3am"
    is answerable after the fact.
    """
    return db_insert(
        """
        INSERT INTO play_sessions
            (guild_id, station_id, station_name, track_title, channel_id,
             requested_by, started_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            int(guild_id),
            str(station_id or ""),
            str(station_name or ""),
            str(track_title or ""),
            int(channel_id) if channel_id else None,
            int(requested_by) if requested_by else None,
            time.time(),
        ),
    )


def end_session(
    session_id: Optional[int],
    *,
    reason: str,
    peak_listeners: int = 0,
    seconds_played: float = 0.0,
) -> None:
    """Close a play-session row. Missing ids are ignored on purpose."""
    if not session_id:
        return
    db_execute(
        """
        UPDATE play_sessions
           SET ended_at = ?, ended_reason = ?, peak_listeners = ?, seconds_played = ?
         WHERE id = ? AND ended_at IS NULL
        """,
        (time.time(), str(reason), int(peak_listeners), float(seconds_played), int(session_id)),
    )


def update_session(
    session_id: Optional[int], *, track_title: Optional[str] = None
) -> None:
    """Refresh the title of an open session as the station moves to a new track.

    A session is one stretch of listening, not one track - a generated station
    plays dozens of tracks per session, and the history should read as "the bot
    played Studio Lofi for three hours" rather than as 240 rows.
    """
    if not session_id or track_title is None:
        return
    db_execute(
        "UPDATE play_sessions SET track_title = ? WHERE id = ? AND ended_at IS NULL",
        (str(track_title), int(session_id)),
    )


def close_dangling_sessions(reason: str = "bot restarted") -> int:
    """Close sessions left open by a crash or a hard kill. Returns how many.

    Without this, the dashboard's history shows sessions that are still
    "playing" forever, and their duration grows every time it is read.
    """
    rows = db_fetchall("SELECT id FROM play_sessions WHERE ended_at IS NULL")
    for row in rows:
        db_execute(
            "UPDATE play_sessions SET ended_at = ?, ended_reason = ? WHERE id = ?",
            (time.time(), str(reason), int(row["id"])),
        )
    return len(rows)


def record_track_play(
    guild_id: int, *, station_id: str, title: str, listeners: int = 0
) -> None:
    """Count one track start, for the "most played" panel."""
    db_insert(
        "INSERT INTO track_plays (guild_id, station_id, title, played_at, listeners) VALUES (?, ?, ?, ?, ?)",
        (int(guild_id), str(station_id or ""), str(title or ""), time.time(), int(listeners)),
    )


def log_event(
    kind: str,
    detail: str = "",
    *,
    guild_id: Optional[int] = None,
    actor_id: Optional[int] = None,
) -> None:
    """Append one line to the activity feed shown in the dashboard."""
    db_insert(
        "INSERT INTO events (guild_id, kind, detail, actor_id, created_at) VALUES (?, ?, ?, ?, ?)",
        (
            int(guild_id) if guild_id else None,
            str(kind),
            str(detail or ""),
            int(actor_id) if actor_id else None,
            time.time(),
        ),
    )


def guild_history(guild_id: Optional[int], limit: int = 100) -> list[dict[str, Any]]:
    """Newest-first play sessions, optionally filtered to one guild."""
    if guild_id is None:
        rows = _fetch(
            "SELECT * FROM play_sessions ORDER BY started_at DESC LIMIT ?", (int(limit),)
        )
    else:
        rows = _fetch(
            "SELECT * FROM play_sessions WHERE guild_id = ? ORDER BY started_at DESC LIMIT ?",
            (int(guild_id), int(limit)),
        )
    return [dict(row) for row in rows]


def guild_events(guild_id: Optional[int], limit: int = 100) -> list[dict[str, Any]]:
    """Newest-first activity events."""
    if guild_id is None:
        rows = _fetch("SELECT * FROM events ORDER BY created_at DESC LIMIT ?", (int(limit),))
    else:
        rows = _fetch(
            "SELECT * FROM events WHERE guild_id = ? ORDER BY created_at DESC LIMIT ?",
            (int(guild_id), int(limit)),
        )
    return [dict(row) for row in rows]


def top_tracks(guild_id: Optional[int] = None, limit: int = 6, days: int = 14) -> list[dict[str, Any]]:
    """Most-played titles in the recent window."""
    since = time.time() - days * 86400
    if guild_id is None:
        rows = db_fetchall(
            """
            SELECT title, COUNT(*) AS plays, MAX(listeners) AS peak_listeners
              FROM track_plays WHERE played_at >= ?
             GROUP BY title ORDER BY plays DESC, title ASC LIMIT ?
            """,
            (since, int(limit)),
        )
    else:
        rows = db_fetchall(
            """
            SELECT title, COUNT(*) AS plays, MAX(listeners) AS peak_listeners
              FROM track_plays WHERE guild_id = ? AND played_at >= ?
             GROUP BY title ORDER BY plays DESC, title ASC LIMIT ?
            """,
            (int(guild_id), since, int(limit)),
        )
    return [dict(row) for row in rows]


def listening_minutes(guild_id: Optional[int] = None, days: int = 7) -> float:
    """Total audio seconds served in the window, as minutes."""
    since = time.time() - days * 86400
    if guild_id is None:
        row = db_fetchone(
            "SELECT COALESCE(SUM(seconds_played), 0) AS total FROM play_sessions WHERE started_at >= ?",
            (since,),
        )
    else:
        row = db_fetchone(
            """
            SELECT COALESCE(SUM(seconds_played), 0) AS total
              FROM play_sessions WHERE guild_id = ? AND started_at >= ?
            """,
            (int(guild_id), since),
        )
    return float(row["total"] or 0.0) / 60.0 if row else 0.0


def session_count(guild_id: Optional[int] = None) -> int:
    """How many play sessions have ever been recorded."""
    if guild_id is None:
        row = db_fetchone("SELECT COUNT(*) AS count FROM play_sessions")
    else:
        row = db_fetchone(
            "SELECT COUNT(*) AS count FROM play_sessions WHERE guild_id = ?", (int(guild_id),)
        )
    return int(row["count"] or 0) if row else 0


__all__ = [
    "SCHEMA",
    "SCHEMA_VERSION",
    "db_execute",
    "db_fetchall",
    "db_fetchone",
    "db_insert",
    "db_path",
    "close_dangling_sessions",
    "end_session",
    "guild_events",
    "guild_history",
    "init_db",
    "listening_minutes",
    "log_event",
    "record_track_play",
    "session_count",
    "start_session",
    "top_tracks",
    "update_session",
]
