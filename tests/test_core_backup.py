from __future__ import annotations

import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import pytest

from alice.core.backup import (
    BackupError,
    BackupService,
    protect_bytes,
    restore_backup,
    unprotect_bytes,
    verify_database_bytes,
)
from alice.core.config import Backup
from alice.core.db import connect, init_db, utc_now

WINDOWS_ONLY = pytest.mark.skipif(
    sys.platform != "win32", reason="DPAPI is Windows-only"
)


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "data"
    directory.mkdir()
    init_db(directory / "alice.db")
    (directory / "memory").mkdir()
    (directory / "memory" / "core.json").write_text(
        '{"identity": {"name": "Jo"}}', encoding="utf-8"
    )
    return directory


@pytest.fixture
def conn(data_dir: Path):
    connection = connect(data_dir / "alice.db", create=False)
    yield connection
    connection.close()


def service(
    conn: sqlite3.Connection, data_dir: Path, backups: Path, *, keep: int = 7
) -> BackupService:
    return BackupService(
        conn,
        data_dir,
        Backup(enabled=True, dir=backups, hour=4, keep=keep),
    )


@WINDOWS_ONLY
def test_dpapi_roundtrip() -> None:
    sealed = protect_bytes(b"secret")

    assert sealed != b"secret"
    assert unprotect_bytes(sealed) == b"secret"


@WINDOWS_ONLY
def test_dpapi_rejects_damaged_ciphertext() -> None:
    sealed = bytearray(protect_bytes(b"secret"))
    sealed[-1] ^= 0xFF

    with pytest.raises(BackupError):
        unprotect_bytes(bytes(sealed))


@WINDOWS_ONLY
def test_backup_is_encrypted_and_recorded(conn, data_dir: Path, tmp_path: Path) -> None:
    backups = tmp_path / "backups"
    moment = datetime(2026, 11, 22, 4, 5).astimezone()
    conn.execute(
        "INSERT INTO events (ts, source, kind, content_hash, text_redacted) "
        "VALUES (?, 'logfile:app.log', 'log', 'h1', 'hidden fact')",
        (utc_now(),),
    )
    conn.commit()

    result = service(conn, data_dir, backups).run(now=moment)

    assert result.ran is True
    assert result.path is not None and result.path.is_file()
    assert result.size_bytes > 0
    assert b"hidden fact" not in result.path.read_bytes()
    row = conn.execute("SELECT * FROM backup_runs").fetchone()
    assert row["day"] == "2026-11-22"
    assert row["path"] == str(result.path)


@WINDOWS_ONLY
def test_same_day_is_skipped_unless_forced(conn, data_dir: Path, tmp_path: Path) -> None:
    backups = tmp_path / "backups"
    service_ = service(conn, data_dir, backups)
    moment = datetime(2026, 11, 22, 4, 5).astimezone()

    assert service_.run(now=moment).ran is True
    second = service_.run(now=moment)
    assert second.ran is False
    assert "already" in (second.reason or "")

    assert service_.run(now=moment, force=True).ran is True
    assert conn.execute("SELECT COUNT(*) FROM backup_runs").fetchone()[0] == 1


@WINDOWS_ONLY
def test_disabled_backup_is_skipped(conn, data_dir: Path, tmp_path: Path) -> None:
    disabled = BackupService(
        conn,
        data_dir,
        Backup(enabled=False, dir=tmp_path / "backups", hour=4, keep=2),
    )

    result = disabled.run()

    assert result.ran is False
    assert "disabled" in (result.reason or "")


@WINDOWS_ONLY
def test_restore_roundtrip(conn, data_dir: Path, tmp_path: Path) -> None:
    backups = tmp_path / "backups"
    conn.execute(
        "INSERT INTO events (ts, source, kind, content_hash, text_redacted) "
        "VALUES (?, 'logfile:app.log', 'log', 'h1', 'survives restore')",
        (utc_now(),),
    )
    conn.commit()
    result = service(conn, data_dir, backups).run(
        now=datetime(2026, 11, 22, 4, 5).astimezone()
    )
    assert result.path is not None

    target = tmp_path / "restored"
    restored = restore_backup(result.path, target)

    assert restored.schema == 8
    assert restored.day == "2026-11-22"
    assert set(restored.files) == {"alice.db", "memory/core.json"}
    restored_conn = connect(target / "alice.db", create=False)
    try:
        row = restored_conn.execute("SELECT text_redacted FROM events").fetchone()
        assert row["text_redacted"] == "survives restore"
    finally:
        restored_conn.close()
    assert (target / "memory" / "core.json").is_file()


@WINDOWS_ONLY
def test_restore_refuses_to_overwrite(conn, data_dir: Path, tmp_path: Path) -> None:
    result = service(conn, data_dir, tmp_path / "backups").run(
        now=datetime(2026, 11, 22, 4, 5).astimezone()
    )
    assert result.path is not None
    target = tmp_path / "existing"
    target.mkdir()
    (target / "alice.db").write_bytes(b"do not replace")

    with pytest.raises(BackupError, match="--force"):
        restore_backup(result.path, target)

    assert (target / "alice.db").read_bytes() == b"do not replace"


@WINDOWS_ONLY
def test_tampered_backup_is_rejected(conn, data_dir: Path, tmp_path: Path) -> None:
    result = service(conn, data_dir, tmp_path / "backups").run(
        now=datetime(2026, 11, 22, 4, 5).astimezone()
    )
    assert result.path is not None
    damaged = result.path.with_name("damaged.alicebak")
    payload = bytearray(result.path.read_bytes())
    payload[-1] ^= 0xFF
    damaged.write_bytes(payload)

    with pytest.raises(BackupError):
        restore_backup(damaged, tmp_path / "restored")


@WINDOWS_ONLY
def test_pruning_keeps_newest_and_cleans_rows(
    conn, data_dir: Path, tmp_path: Path
) -> None:
    backups = tmp_path / "backups"
    service_ = service(conn, data_dir, backups, keep=2)
    for day in (20, 21, 22):
        service_.run(now=datetime(2026, 11, day, 4, 5).astimezone(), force=True)

    files = sorted(path.name for path in backups.glob("*.alicebak"))
    assert files == ["alice-2026-11-21.alicebak", "alice-2026-11-22.alicebak"]
    days = [row["day"] for row in conn.execute("SELECT day FROM backup_runs")]
    assert days == ["2026-11-21", "2026-11-22"]


def test_verify_rejects_non_sqlite() -> None:
    with pytest.raises(BackupError, match="SQLite"):
        verify_database_bytes(b"not a database")
