"""Encrypted local backups and restore.

Backups are a ZIP containing a consistent SQLite snapshot and the editable core
memory file, encrypted with Windows DPAPI. DPAPI ties the file to the current
Windows user, so there is no password or key to store in configuration. The
file can only be restored by the same user on the same machine (or a machine
where the user's profile was migrated with the DPAPI master keys).

The service records one row per local day in `backup_runs`, prunes old files,
and can verify and restore a backup into an empty data directory.
"""

from __future__ import annotations

import io
import json
import logging
import os
import sqlite3
import sys
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .. import __version__
from .config import Backup
from .db import DatabaseError, connect, current_version, init_db, utc_now

logger = logging.getLogger(__name__)

MAGIC = b"ALICEBK1\n"
ENTROPY = b"alice-backup-v1"
DB_MEMBER = "alice.db"
CORE_MEMBER = "memory/core.json"
MANIFEST_MEMBER = "manifest.json"


class BackupError(RuntimeError):
    """A backup or restore could not be completed safely."""


class BackupProtectionError(BackupError):
    """DPAPI encryption or decryption failed."""


@dataclass(frozen=True)
class BackupResult:
    ran: bool
    day: str
    path: Path | None = None
    size_bytes: int = 0
    kept: int = 0
    reason: str | None = None


@dataclass(frozen=True)
class RestoreResult:
    path: Path
    target: Path
    day: str | None
    schema: int
    files: tuple[str, ...]


def protect_bytes(data: bytes, *, entropy: bytes = ENTROPY) -> bytes:
    """Encrypt bytes for the current Windows user with DPAPI."""
    if sys.platform != "win32":
        raise BackupProtectionError(
            "encrypted backups use Windows DPAPI and require Windows"
        )
    import ctypes
    from ctypes import wintypes

    class DataBlob(ctypes.Structure):
        _fields_ = [
            ("cbData", wintypes.DWORD),
            ("pbData", ctypes.POINTER(ctypes.c_char)),
        ]

    def make_blob(payload: bytes) -> tuple[DataBlob, ctypes.Array[ctypes.c_char]]:
        buffer = ctypes.create_string_buffer(payload, len(payload))
        blob = DataBlob(
            len(payload), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char))
        )
        return blob, buffer

    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(DataBlob),
        wintypes.LPCWSTR,
        ctypes.POINTER(DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(DataBlob),
    ]
    crypt32.CryptProtectData.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p

    input_blob, input_buffer = make_blob(data)
    entropy_blob, entropy_buffer = make_blob(entropy)
    output_blob = DataBlob()
    ok = crypt32.CryptProtectData(
        ctypes.byref(input_blob),
        "Alice backup",
        ctypes.byref(entropy_blob),
        None,
        None,
        0x1,  # CRYPTPROTECT_UI_FORBIDDEN
        ctypes.byref(output_blob),
    )
    _ = (input_buffer, entropy_buffer)
    if not ok:
        raise BackupProtectionError(
            f"DPAPI encryption failed: {ctypes.get_last_error()}"
        )
    try:
        return ctypes.string_at(output_blob.pbData, output_blob.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(output_blob.pbData, ctypes.c_void_p))


def unprotect_bytes(data: bytes, *, entropy: bytes = ENTROPY) -> bytes:
    """Decrypt bytes previously protected for the current Windows user."""
    if sys.platform != "win32":
        raise BackupProtectionError(
            "encrypted backups use Windows DPAPI and require Windows"
        )
    import ctypes
    from ctypes import wintypes

    class DataBlob(ctypes.Structure):
        _fields_ = [
            ("cbData", wintypes.DWORD),
            ("pbData", ctypes.POINTER(ctypes.c_char)),
        ]

    def make_blob(payload: bytes) -> tuple[DataBlob, ctypes.Array[ctypes.c_char]]:
        buffer = ctypes.create_string_buffer(payload, len(payload))
        blob = DataBlob(
            len(payload), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char))
        )
        return blob, buffer

    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(DataBlob),
        ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(DataBlob),
    ]
    crypt32.CryptUnprotectData.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p

    input_blob, input_buffer = make_blob(data)
    entropy_blob, entropy_buffer = make_blob(entropy)
    output_blob = DataBlob()
    ok = crypt32.CryptUnprotectData(
        ctypes.byref(input_blob),
        None,
        ctypes.byref(entropy_blob),
        None,
        None,
        0x1,  # CRYPTPROTECT_UI_FORBIDDEN
        ctypes.byref(output_blob),
    )
    _ = (input_buffer, entropy_buffer)
    if not ok:
        raise BackupProtectionError(
            "DPAPI decryption failed; the file was made by another Windows "
            f"user or is damaged ({ctypes.get_last_error()})"
        )
    try:
        return ctypes.string_at(output_blob.pbData, output_blob.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(output_blob.pbData, ctypes.c_void_p))


def _snapshot_bytes(db_path: Path) -> bytes:
    """Use SQLite's online backup API so WAL content is included."""
    with tempfile.TemporaryDirectory(prefix="alice-backup-") as folder:
        target_path = Path(folder) / DB_MEMBER
        try:
            source = sqlite3.connect(str(db_path))
        except sqlite3.Error as exc:
            raise BackupError(f"cannot open {db_path}: {exc}") from exc
        try:
            target = sqlite3.connect(target_path)
            try:
                source.backup(target)
            finally:
                target.close()
        except sqlite3.Error as exc:
            raise BackupError(f"database snapshot failed: {exc}") from exc
        finally:
            source.close()
        return target_path.read_bytes()


def _build_archive(db_path: Path, core_path: Path, day: str) -> bytes:
    database = _snapshot_bytes(db_path)
    core_bytes = core_path.read_bytes() if core_path.is_file() else None
    members = [DB_MEMBER] + ([CORE_MEMBER] if core_bytes is not None else [])
    manifest = {
        "kind": "alice-backup",
        "format": 1,
        "day": day,
        "created": utc_now(),
        "alice_version": __version__,
        "members": members,
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(MANIFEST_MEMBER, json.dumps(manifest, ensure_ascii=False))
        archive.writestr(DB_MEMBER, database)
        if core_bytes is not None:
            archive.writestr(CORE_MEMBER, core_bytes)
    return buffer.getvalue()


def _read_archive(data: bytes) -> tuple[dict, dict[str, bytes]]:
    if not data.startswith(MAGIC):
        raise BackupError("not an Alice backup file")
    plain = unprotect_bytes(data[len(MAGIC) :])
    try:
        with zipfile.ZipFile(io.BytesIO(plain)) as archive:
            names = archive.namelist()
            for name in names:
                if name.startswith("/") or ".." in Path(name).parts or "\\" in name:
                    raise BackupError(f"unsafe path in backup: {name}")
            members = {
                name: archive.read(name)
                for name in names
                if name in (DB_MEMBER, CORE_MEMBER, MANIFEST_MEMBER)
            }
    except zipfile.BadZipFile as exc:
        raise BackupError(f"backup is not a readable ZIP archive: {exc}") from exc
    if DB_MEMBER not in members:
        raise BackupError("backup does not contain alice.db")
    manifest: dict = {}
    try:
        loaded = json.loads(members.get(MANIFEST_MEMBER, b"{}"))
        if isinstance(loaded, dict):
            manifest = loaded
    except ValueError:
        manifest = {}
    return manifest, members


def verify_database_bytes(data: bytes) -> int:
    """Write a snapshot to a temporary file, check integrity, return schema."""
    if not data.startswith(b"SQLite format 3\x00"):
        raise BackupError("backup database is not a SQLite file")
    with tempfile.TemporaryDirectory(prefix="alice-restore-") as folder:
        path = Path(folder) / DB_MEMBER
        path.write_bytes(data)
        try:
            conn = connect(path, create=False)
        except (DatabaseError, sqlite3.Error) as exc:
            raise BackupError(f"cannot open backup database: {exc}") from exc
        try:
            row = conn.execute("PRAGMA integrity_check").fetchone()
            if row is None or str(row[0]).lower() != "ok":
                raise BackupError(f"backup database failed integrity check: {row[0]}")
            return current_version(conn)
        finally:
            conn.close()


class BackupService:
    def __init__(
        self,
        conn: sqlite3.Connection,
        data_dir: Path | str,
        config: Backup,
    ) -> None:
        self.conn = conn
        self.data_dir = Path(data_dir)
        self.config = config
        self.db_path = self.data_dir / "alice.db"
        self.core_path = self.data_dir / "memory" / "core.json"

    def last_day(self) -> str | None:
        row = self.conn.execute(
            "SELECT day FROM backup_runs ORDER BY day DESC LIMIT 1"
        ).fetchone()
        return None if row is None else str(row[0])

    def run(self, *, now: datetime | None = None, force: bool = False) -> BackupResult:
        moment = now or datetime.now().astimezone()
        day = moment.astimezone().date().isoformat()
        if not self.config.enabled and not force:
            return BackupResult(ran=False, day=day, reason="backups are disabled")
        if not force and self.last_day() == day:
            return BackupResult(ran=False, day=day, reason="already backed up today")
        if not self.db_path.is_file():
            raise BackupError(f"database not found: {self.db_path}")

        archive = _build_archive(self.db_path, self.core_path, day)
        sealed = MAGIC + protect_bytes(archive)

        self.config.dir.mkdir(parents=True, exist_ok=True)
        path = self.config.dir / f"alice-{day}.alicebak"
        temp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        try:
            temp.write_bytes(sealed)
            os.replace(temp, path)
        except OSError as exc:
            temp.unlink(missing_ok=True)
            raise BackupError(f"cannot write backup {path}: {exc}") from exc

        size = path.stat().st_size
        self.conn.execute(
            """
            INSERT INTO backup_runs (day, created, path, size_bytes)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(day) DO UPDATE SET
              created = excluded.created,
              path = excluded.path,
              size_bytes = excluded.size_bytes
            """,
            (day, utc_now(), str(path), size),
        )
        self.conn.commit()
        kept = self._prune()
        logger.info("backup written: %s (%d bytes)", path, size)
        return BackupResult(
            ran=True, day=day, path=path, size_bytes=size, kept=kept
        )

    def list_files(self) -> list[Path]:
        if not self.config.dir.is_dir():
            return []
        return sorted(
            self.config.dir.glob("alice-*.alicebak"),
            key=lambda item: item.name,
            reverse=True,
        )

    def _prune(self) -> int:
        files = self.list_files()
        keep = max(1, self.config.keep)
        stale = files[keep:]
        for path in stale:
            try:
                path.unlink()
            except OSError:
                logger.warning("could not delete old backup %s", path)
        kept_names = {str(path) for path in files[:keep]}
        rows = self.conn.execute("SELECT day, path FROM backup_runs").fetchall()
        for row in rows:
            if str(row["path"]) not in kept_names:
                self.conn.execute(
                    "DELETE FROM backup_runs WHERE day = ?", (row["day"],)
                )
        self.conn.commit()
        return len(files[:keep])


def restore_backup(
    backup_path: Path | str,
    data_dir: Path | str,
    *,
    force: bool = False,
) -> RestoreResult:
    """Verify and restore a backup into a data directory."""
    source = Path(backup_path)
    try:
        sealed = source.read_bytes()
    except OSError as exc:
        raise BackupError(f"cannot read backup {source}: {exc}") from exc

    manifest, members = _read_archive(sealed)
    verify_database_bytes(members[DB_MEMBER])

    target_dir = Path(data_dir)
    target_db = target_dir / "alice.db"
    if target_db.exists() and not force:
        raise BackupError(
            f"{target_db} already exists; pass --force to replace it"
        )

    target_dir.mkdir(parents=True, exist_ok=True)
    temp = target_dir / f".alice-restore-{os.getpid()}.db"
    try:
        temp.write_bytes(members[DB_MEMBER])
        for suffix in ("-wal", "-shm"):
            target_db.with_name(target_db.name + suffix).unlink(missing_ok=True)
        os.replace(temp, target_db)
    except OSError as exc:
        temp.unlink(missing_ok=True)
        raise BackupError(f"cannot restore {target_db}: {exc}") from exc

    restored: list[str] = [DB_MEMBER]
    core_bytes = members.get(CORE_MEMBER)
    if core_bytes is not None:
        core_path = target_dir / "memory" / "core.json"
        core_path.parent.mkdir(parents=True, exist_ok=True)
        core_temp = core_path.with_name(f"{core_path.name}.{os.getpid()}.tmp")
        try:
            core_temp.write_bytes(core_bytes)
            os.replace(core_temp, core_path)
        except OSError as exc:
            core_temp.unlink(missing_ok=True)
            raise BackupError(f"cannot restore {core_path}: {exc}") from exc
        restored.append(CORE_MEMBER)

    try:
        result = init_db(target_db)
    except DatabaseError as exc:
        raise BackupError(f"restored database is not usable: {exc}") from exc

    return RestoreResult(
        path=source,
        target=target_dir,
        day=manifest.get("day"),
        schema=result.version,
        files=tuple(restored),
    )


def format_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} GB"
