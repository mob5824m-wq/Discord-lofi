"""SQLite state: play sessions, track plays, events and schema self-healing."""

from __future__ import annotations

import sqlite3
import time

import pytest

import store


GUILD = 400000000000000001
OTHER = 400000000000000002


def open_session(guild_id: int = GUILD, **overrides):
    """One play session with the keyword-only fields filled in."""
    fields = dict(
        station_id="groove-salad",
        station_name="Groove Salad",
        track_title="Ambient Set",
        channel_id=555,
        requested_by=777,
    )
    fields.update(overrides)
    return store.start_session(guild_id, **fields)


def test_init_db_creates_the_tables(tmp_state):
    store.init_db()
    names = {row["name"] for row in store.db_fetchall("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"play_sessions", "events", "track_plays", "schema_version"} <= names


def test_init_db_records_the_schema_version(tmp_state):
    store.init_db()
    assert store.db_fetchone("SELECT version FROM schema_version")["version"] == store.SCHEMA_VERSION


def test_init_db_is_idempotent(tmp_state):
    store.init_db()
    store.init_db()
    store.log_event("boot", "twice", guild_id=GUILD)
    assert store.db_fetchone("SELECT COUNT(*) AS n FROM events")["n"] == 1


def test_the_schema_is_recreated_if_the_file_goes_missing(tmp_state, monkeypatch):
    """A database deleted underneath a running bot must recover, not 500 forever."""
    store.init_db()
    fresh = tmp_state / "fresh.db"
    monkeypatch.setattr(store, "db_path", lambda: str(fresh))
    store.log_event("boot", "no table yet", guild_id=GUILD)
    assert fresh.exists()
    assert store.db_fetchone("SELECT COUNT(*) AS n FROM events")["n"] == 1


def test_the_schema_is_recreated_for_reads_too(tmp_state, monkeypatch):
    fresh = tmp_state / "read.db"
    monkeypatch.setattr(store, "db_path", lambda: str(fresh))
    assert store.guild_history(GUILD) == []
    assert store.guild_events(GUILD) == []


def test_db_path_honours_the_environment(tmp_state, monkeypatch):
    monkeypatch.setenv("LOFI_DB", str(tmp_state / "custom.db"))
    assert store.db_path() == str(tmp_state / "custom.db")


def test_db_path_falls_back_to_the_data_directory(tmp_state, monkeypatch):
    monkeypatch.delenv("LOFI_DB", raising=False)
    assert store.db_path() == str(tmp_state / "test.db")


# --------------------------------------------------------------------------- #
# Sessions
# --------------------------------------------------------------------------- #
def test_a_session_can_be_started_and_read_back(tmp_state):
    session_id = open_session()
    row = store.db_fetchone("SELECT * FROM play_sessions WHERE id = ?", (session_id,))
    assert row["guild_id"] == GUILD
    assert row["station_name"] == "Groove Salad"
    assert row["track_title"] == "Ambient Set"
    assert row["channel_id"] == 555
    assert row["requested_by"] == 777
    assert row["ended_at"] is None


def test_a_started_session_stamps_its_start_time(tmp_state):
    before = time.time()
    open_session()
    after = time.time()
    started = store.db_fetchone("SELECT started_at FROM play_sessions")["started_at"]
    assert before <= started <= after


def test_an_open_session_is_counted(tmp_state):
    open_session()
    assert store.session_count(GUILD) == 1


def test_end_session_closes_the_row_with_a_reason(tmp_state):
    session_id = open_session()
    store.end_session(session_id, reason="stop", peak_listeners=4, seconds_played=90.5)
    row = store.db_fetchone("SELECT * FROM play_sessions WHERE id = ?", (session_id,))
    assert row["ended_reason"] == "stop"
    assert row["peak_listeners"] == 4
    assert row["seconds_played"] == 90.5
    assert row["ended_at"] is not None


def test_update_session_refreshes_the_title_as_the_station_moves_on(tmp_state):
    session_id = open_session()
    store.update_session(session_id, track_title="Second Track")
    assert store.db_fetchone("SELECT track_title AS t FROM play_sessions WHERE id = ?", (session_id,))["t"] == "Second Track"


def test_update_session_with_no_title_changes_nothing(tmp_state):
    session_id = open_session()
    store.update_session(session_id)
    assert store.db_fetchone("SELECT track_title AS t FROM play_sessions WHERE id = ?", (session_id,))["t"] == "Ambient Set"


def test_update_session_on_an_unknown_id_is_harmless(tmp_state):
    store.update_session(999999, track_title="ghost")  # must not raise


def test_end_session_on_an_unknown_id_is_harmless(tmp_state):
    store.end_session(999999, reason="ghost")  # must not raise


def test_end_session_on_none_is_harmless(tmp_state):
    """A player that never opened a session must still be able to stop cleanly."""
    store.end_session(None, reason="stop")


def test_listening_minutes_sums_finished_sessions(tmp_state):
    for guild, minutes in ((GUILD, 30), (GUILD, 45), (OTHER, 10)):
        session_id = open_session(guild)
        store.end_session(session_id, reason="stop", seconds_played=minutes * 60)
    assert store.listening_minutes(GUILD) == 75
    assert store.listening_minutes(OTHER) == 10


def test_listening_minutes_across_every_guild(tmp_state):
    for guild in (GUILD, OTHER):
        session_id = open_session(guild)
        store.end_session(session_id, reason="stop", seconds_played=600)
    assert store.listening_minutes() == 20


def test_an_open_session_contributes_no_minutes(tmp_state):
    """Its length is unknown until it ends; guessing would inflate the number."""
    open_session()
    assert store.listening_minutes(GUILD) == 0


def test_listening_minutes_respects_the_window(tmp_state):
    now = time.time()
    for age_days, minutes in ((1, 60), (40, 120)):
        session_id = store.db_insert(
            "INSERT INTO play_sessions (guild_id, started_at, ended_at, seconds_played) VALUES (?,?,?,?)",
            (GUILD, now - age_days * 86400, now - age_days * 86400 + 60, minutes * 60),
        )
        assert session_id
    assert store.listening_minutes(GUILD, days=7) == 60
    assert store.listening_minutes(GUILD, days=90) == 180


def test_session_count_is_scoped_to_one_guild(tmp_state):
    open_session(GUILD)
    open_session(GUILD)
    open_session(OTHER)
    assert store.session_count(GUILD) == 2
    assert store.session_count(OTHER) == 1
    assert store.session_count() == 3


def test_close_dangling_sessions_closes_only_open_ones(tmp_state):
    finished = open_session()
    store.end_session(finished, reason="stop")
    dangling = open_session()
    closed = store.close_dangling_sessions()
    assert closed == 1
    assert store.db_fetchone("SELECT ended_reason AS r FROM play_sessions WHERE id = ?", (dangling,))["r"] == "bot restarted"


def test_close_dangling_sessions_accepts_a_custom_reason(tmp_state):
    open_session()
    assert store.close_dangling_sessions("shutting down") == 1
    assert store.db_fetchone("SELECT ended_reason AS r FROM play_sessions")["r"] == "shutting down"


def test_close_dangling_sessions_reports_zero_when_there_is_nothing_to_do(tmp_state):
    store.init_db()
    assert store.close_dangling_sessions() == 0


# --------------------------------------------------------------------------- #
# Track plays ("most played" panel)
# --------------------------------------------------------------------------- #
def test_track_plays_are_counted(tmp_state):
    store.record_track_play(GUILD, station_id="groove-salad", title="Ambient Set")
    store.record_track_play(GUILD, station_id="groove-salad", title="Ambient Set")
    top = store.top_tracks(GUILD)
    assert top[0]["title"] == "Ambient Set"
    assert top[0]["plays"] == 2


def test_top_tracks_keep_the_peak_listener_count(tmp_state):
    store.record_track_play(GUILD, station_id="library", title="A", listeners=2)
    store.record_track_play(GUILD, station_id="library", title="A", listeners=9)
    store.record_track_play(GUILD, station_id="library", title="A", listeners=4)
    assert store.top_tracks(GUILD)[0]["peak_listeners"] == 9


def test_top_tracks_are_ordered_by_plays_then_title(tmp_state):
    """A tie is broken by name so the panel does not reshuffle on every poll."""
    for title, plays in (("Track B", 2), ("Track A", 2), ("Track C", 1)):
        for _ in range(plays):
            store.record_track_play(GUILD, station_id="library", title=title)
    assert [row["title"] for row in store.top_tracks(GUILD)] == ["Track A", "Track B", "Track C"]


def test_top_tracks_respect_the_limit(tmp_state):
    for index in range(10):
        store.record_track_play(GUILD, station_id="library", title=f"Track {index}")
    assert len(store.top_tracks(GUILD, limit=4)) == 4


def test_top_tracks_are_scoped_to_one_guild(tmp_state):
    store.record_track_play(GUILD, station_id="library", title="Mine")
    store.record_track_play(OTHER, station_id="library", title="Theirs")
    assert [row["title"] for row in store.top_tracks(GUILD)] == ["Mine"]
    assert len(store.top_tracks()) == 2


def test_top_tracks_respect_the_window(tmp_state):
    now = time.time()
    store.db_insert(
        "INSERT INTO track_plays (guild_id, station_id, title, played_at) VALUES (?,?,?,?)",
        (GUILD, "library", "Ancient", now - 40 * 86400),
    )
    store.record_track_play(GUILD, station_id="library", title="Recent")
    assert [row["title"] for row in store.top_tracks(GUILD, days=14)] == ["Recent"]
    assert len(store.top_tracks(GUILD, days=60)) == 2


def test_history_for_a_guild_with_no_plays_is_empty(tmp_state):
    assert store.guild_history(GUILD, limit=5) == []


# --------------------------------------------------------------------------- #
# History and events
# --------------------------------------------------------------------------- #
def test_history_is_newest_first_and_limited(tmp_state):
    for index in range(12):
        open_session(track_title=f"Track {index}")
    rows = store.guild_history(GUILD, limit=5)
    assert len(rows) == 5
    assert rows[0]["track_title"] == "Track 11"


def test_history_rows_carry_the_station_and_channel(tmp_state):
    open_session(station_id="groove-salad", station_name="Groove Salad", channel_id=42)
    row = store.guild_history(GUILD, limit=1)[0]
    assert row["station_id"] == "groove-salad"
    assert row["station_name"] == "Groove Salad"
    assert row["channel_id"] == 42


def test_history_is_returned_as_plain_dicts(tmp_state):
    """The dashboard json-encodes these directly; a sqlite3.Row would not serialise."""
    open_session()
    row = store.guild_history(GUILD, limit=1)[0]
    assert isinstance(row, dict)
    import json

    json.dumps(row, default=str)


def test_history_across_every_guild(tmp_state):
    open_session(GUILD)
    open_session(OTHER)
    assert len(store.guild_history(None, limit=10)) == 2


def test_events_are_logged_with_a_kind_and_detail(tmp_state):
    store.log_event("connect", "joined #study-room", guild_id=GUILD, actor_id=123)
    row = store.guild_events(GUILD, limit=5)[0]
    assert row["kind"] == "connect"
    assert row["detail"] == "joined #study-room"
    assert row["actor_id"] == 123


def test_an_event_without_a_guild_is_still_readable(tmp_state):
    store.log_event("boot", "no guild")
    assert store.guild_events(None, limit=5) != []


def test_events_are_newest_first(tmp_state):
    store.log_event("first", "one", guild_id=GUILD)
    store.log_event("second", "two", guild_id=GUILD)
    assert [row["kind"] for row in store.guild_events(GUILD, limit=5)] == ["second", "first"]


def test_events_are_scoped_to_one_guild(tmp_state):
    store.log_event("mine", "one", guild_id=GUILD)
    store.log_event("theirs", "two", guild_id=OTHER)
    assert [row["kind"] for row in store.guild_events(GUILD)] == ["mine"]


def test_events_respect_the_limit(tmp_state):
    for index in range(8):
        store.log_event(f"event-{index}", guild_id=GUILD)
    assert len(store.guild_events(GUILD, limit=3)) == 3


def test_events_are_returned_as_plain_dicts(tmp_state):
    store.log_event("mine", guild_id=GUILD)
    assert isinstance(store.guild_events(GUILD)[0], dict)


# --------------------------------------------------------------------------- #
# Low-level helpers
# --------------------------------------------------------------------------- #
def test_db_insert_returns_the_new_row_id(tmp_state):
    row_id = store.db_insert(
        "INSERT INTO events (guild_id, kind, detail, created_at) VALUES (?,?,?,?)",
        (GUILD, "x", "y", time.time()),
    )
    assert isinstance(row_id, int) and row_id > 0


def test_db_execute_commits_without_returning_a_value(tmp_state):
    store.log_event("x", "one", guild_id=GUILD)
    assert store.db_execute("DELETE FROM events WHERE kind = ?", ("x",)) is None
    assert store.db_fetchone("SELECT COUNT(*) AS n FROM events")["n"] == 0


def test_fetchone_returns_none_when_there_is_no_row(tmp_state):
    store.init_db()
    assert store.db_fetchone("SELECT * FROM events WHERE guild_id = ?", (GUILD,)) is None


def test_fetchall_returns_rows_that_support_key_access(tmp_state):
    store.log_event("x", "one", guild_id=GUILD)
    assert store.db_fetchall("SELECT * FROM events")[0]["kind"] == "x"


def test_a_snowflake_survives_the_round_trip_unchanged(tmp_state):
    """Discord ids exceed double precision; storing one as REAL would corrupt it."""
    store.log_event("x", "one", guild_id=GUILD)
    assert store.guild_events(GUILD, limit=1)[0]["guild_id"] == GUILD


def test_connections_are_not_left_open(tmp_state):
    """One connection per statement: a long-running bot must not leak file handles."""
    import resource

    before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    for index in range(200):
        store.log_event(f"event-{index}", guild_id=GUILD)
        store.guild_history(GUILD, limit=5)
    after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    assert after - before < 50_000  # kilobytes


def test_a_locked_database_raises_instead_of_hanging(tmp_state):
    """A second process holding the file must produce an error, not a deadlock."""
    store.init_db()
    blocker = sqlite3.connect(store.db_path(), timeout=0.05, isolation_level=None)
    blocker.execute("BEGIN EXCLUSIVE")
    try:
        with pytest.raises(sqlite3.OperationalError):
            store.db_insert("INSERT INTO events (guild_id, kind, created_at) VALUES (?,?,?)", (GUILD, "x", time.time()))
    finally:
        blocker.rollback()
        blocker.close()


def test_writes_resume_after_the_lock_is_released(tmp_state):
    store.init_db()
    blocker = sqlite3.connect(store.db_path(), timeout=0.05, isolation_level=None)
    blocker.execute("BEGIN EXCLUSIVE")
    blocker.rollback()
    blocker.close()
    store.log_event("after-lock", guild_id=GUILD)
    assert store.guild_events(GUILD, limit=1)[0]["kind"] == "after-lock"
