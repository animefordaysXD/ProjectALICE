from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from alice.core.db import (
    DatabaseError,
    connect,
    count_rows,
    database_status,
    init_db,
    pending_migrations,
    table_names,
    utc_now,
)

EXPECTED_TABLES = {
    "actions",
    "ambient_alerts",
    "backup_runs",
    "budget_alerts",
    "chat_messages",
    "consolidation_runs",
    "cursors",
    "events",
    "growth_memories",
    "growth_sessions",
    "hard_memory",
    "memory_embeddings",
    "proposals",
    "suggestions",
    "summaries",
    "tasks",
    "tool_calls",
    "triage_batches",
    "usage",
}


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "state" / "alice.db"


def insert_event(conn: sqlite3.Connection, text: str, *, content_hash: str = "hash-1") -> int:
    cursor = conn.execute(
        """
        INSERT INTO events (ts, source, kind, content_hash, text_redacted)
        VALUES (?, 'logfile:test.log', 'log', ?, ?)
        """,
        (utc_now(), content_hash, text),
    )
    conn.commit()
    return int(cursor.lastrowid)


def test_init_creates_every_table(db_path: Path) -> None:
    result = init_db(db_path)

    assert result.version == 13
    assert [migration.version for migration in result.applied] == [
        1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13
    ]
    assert db_path.is_file()

    conn = connect(db_path, create=False)
    try:
        assert set(table_names(conn)) == EXPECTED_TABLES
        internal = table_names(conn, include_internal=True)
        assert "events_fts" in internal
        assert "summaries_fts" in internal
    finally:
        conn.close()


def test_init_is_idempotent(db_path: Path) -> None:
    init_db(db_path)
    result = init_db(db_path)

    assert result.applied == ()
    assert result.version == 13


def test_database_uses_wal(db_path: Path) -> None:
    init_db(db_path)
    conn = connect(db_path, create=False)
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    finally:
        conn.close()


def test_refuses_foreign_database(tmp_path: Path) -> None:
    foreign = tmp_path / "someone-elses.db"
    conn = sqlite3.connect(foreign)
    conn.execute("CREATE TABLE notes (body TEXT)")
    conn.execute("INSERT INTO notes VALUES ('do not touch')")
    conn.commit()
    conn.close()

    with pytest.raises(DatabaseError, match="not created by Alice"):
        init_db(foreign)

    conn = sqlite3.connect(foreign)
    try:
        assert conn.execute("SELECT body FROM notes").fetchone()[0] == "do not touch"
        assert not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'events'"
        ).fetchone()
    finally:
        conn.close()


def test_refuses_database_from_newer_build(db_path: Path) -> None:
    init_db(db_path)
    conn = connect(db_path, create=False)
    try:
        conn.execute(
            "INSERT INTO schema_migrations (version, name, applied) VALUES (99, 'future', ?)",
            (utc_now(),),
        )
        conn.commit()
        with pytest.raises(DatabaseError, match="newer than this build"):
            pending_migrations(conn)
    finally:
        conn.close()


def test_status_missing_database(db_path: Path) -> None:
    state = database_status(db_path)

    assert state.initialized is False
    assert state.version is None
    assert state.tables == ()


def test_status_after_init(db_path: Path) -> None:
    init_db(db_path)
    state = database_status(db_path)

    assert state.initialized is True
    assert state.version == 13
    assert state.pending == ()
    assert set(state.tables) == EXPECTED_TABLES


def test_status_reports_pending_after_new_migration(db_path: Path, tmp_path: Path) -> None:
    init_db(db_path)

    package_migrations = (
        Path(__file__).resolve().parents[1] / "src" / "alice" / "core" / "migrations"
    )
    test_migrations = tmp_path / "migrations"
    test_migrations.mkdir()
    for path in package_migrations.glob("*.sql"):
        (test_migrations / path.name).write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    next_version = max(int(path.name.split("_")[0]) for path in package_migrations.glob("*.sql")) + 1
    (test_migrations / f"{next_version:03d}_pending_example.sql").write_text(
        "CREATE TABLE example (id INTEGER PRIMARY KEY);", encoding="utf-8"
    )

    state = database_status(db_path, migrations_dir=test_migrations)

    assert state.initialized is True
    assert state.version == 13
    assert state.pending == (next_version,)


def test_events_fts_stays_in_sync(db_path: Path) -> None:
    init_db(db_path)
    conn = connect(db_path, create=False)
    try:
        event_id = insert_event(conn, "database backup failed 서버 오류")

        found = conn.execute(
            "SELECT rowid FROM events_fts WHERE events_fts MATCH ?", ("backup",)
        ).fetchall()
        assert [row[0] for row in found] == [event_id]
        assert conn.execute(
            "SELECT rowid FROM events_fts WHERE events_fts MATCH ?", ("서버",)
        ).fetchone() is not None

        conn.execute(
            "UPDATE events SET text_redacted = ? WHERE id = ?",
            ("disk is full 디스크 가득", event_id),
        )
        conn.commit()
        assert conn.execute(
            "SELECT rowid FROM events_fts WHERE events_fts MATCH ?", ("backup",)
        ).fetchone() is None
        assert conn.execute(
            "SELECT rowid FROM events_fts WHERE events_fts MATCH ?", ("디스크",)
        ).fetchone() is not None

        conn.execute("DELETE FROM events WHERE id = ?", (event_id,))
        conn.commit()
        assert conn.execute(
            "SELECT rowid FROM events_fts WHERE events_fts MATCH ?", ("디스크",)
        ).fetchone() is None
    finally:
        conn.close()


def test_summaries_fts_stays_in_sync(db_path: Path) -> None:
    init_db(db_path)
    conn = connect(db_path, create=False)
    try:
        conn.execute(
            "INSERT INTO summaries (level, period_start, period_end, text) VALUES (?, ?, ?, ?)",
            ("day", "2026-11-20T00:00:00Z", "2026-11-21T00:00:00Z", "정기 점검과 배포"),
        )
        conn.commit()
        assert conn.execute(
            "SELECT rowid FROM summaries_fts WHERE summaries_fts MATCH ?", ("배포",)
        ).fetchone() is not None
    finally:
        conn.close()


def test_count_rows_validates_table_name(db_path: Path) -> None:
    init_db(db_path)
    conn = connect(db_path, create=False)
    try:
        assert count_rows(conn, "events") == 0
        with pytest.raises(DatabaseError, match="invalid table name"):
            count_rows(conn, "events; DROP TABLE events")
        with pytest.raises(DatabaseError, match="unknown table"):
            count_rows(conn, "not_a_table")
    finally:
        conn.close()


def test_unique_content_hash(db_path: Path) -> None:
    init_db(db_path)
    conn = connect(db_path, create=False)
    try:
        insert_event(conn, "first", content_hash="same")
        with pytest.raises(sqlite3.IntegrityError):
            insert_event(conn, "second", content_hash="same")
    finally:
        conn.close()

