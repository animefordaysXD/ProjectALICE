"""SQLite connection handling, migrations, and status helpers.

One database file holds everything. Migrations are plain `.sql` files in
`alice/core/migrations`, applied in filename order and recorded in
`schema_migrations`.
"""

from __future__ import annotations

import re
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

MIGRATIONS_DIR = (
    Path(sys._MEIPASS) / "alice" / "core" / "migrations"  # type: ignore[attr-defined]
    if getattr(sys, "_MEIPASS", None)
    else Path(__file__).resolve().parent / "migrations"
)
SCHEMA_TABLE = "schema_migrations"

_MIGRATION_FILE_RE = re.compile(r"^(\d{3,})_([a-z0-9_]+)\.sql$")
_SAFE_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class DatabaseError(RuntimeError):
    """Raised when the database cannot be opened or migrated safely."""


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str
    path: Path


@dataclass(frozen=True)
class MigrationResult:
    version: int
    applied: tuple[Migration, ...]


@dataclass(frozen=True)
class DatabaseStatus:
    initialized: bool
    version: int | None
    pending: tuple[int, ...]
    tables: tuple[str, ...]


def utc_now() -> str:
    """Current UTC time as ISO 8601, e.g. 2026-11-21T09:30:00Z."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect(db_path: Path | str, *, create: bool = True) -> sqlite3.Connection:
    """Open the database with Alice's pragmas applied."""
    path = Path(db_path)
    if create:
        path.parent.mkdir(parents=True, exist_ok=True)
    elif not path.is_file():
        raise DatabaseError(f"database not found: {path}")

    try:
        conn = sqlite3.connect(path, timeout=5.0)
    except sqlite3.Error as exc:
        raise DatabaseError(f"cannot open database {path}: {exc}") from exc

    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")
    except sqlite3.Error as exc:
        conn.close()
        raise DatabaseError(f"cannot configure database {path}: {exc}") from exc
    return conn


def init_db(db_path: Path | str, *, migrations_dir: Path | None = None) -> MigrationResult:
    """Create or migrate the database. Safe to run repeatedly."""
    conn = connect(db_path)
    try:
        return migrate(conn, migrations_dir=migrations_dir)
    finally:
        conn.close()


def migrate(
    conn: sqlite3.Connection, *, migrations_dir: Path | None = None
) -> MigrationResult:
    """Apply every pending migration, one transaction per migration."""
    ensure_schema_table(conn)
    if _is_foreign_database(conn):
        raise DatabaseError(
            "refusing to migrate: the file contains tables but was not created by Alice"
        )

    applied: list[Migration] = []
    for migration in pending_migrations(conn, migrations_dir=migrations_dir):
        _apply_migration(conn, migration)
        applied.append(migration)

    return MigrationResult(version=current_version(conn), applied=tuple(applied))


def ensure_schema_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {SCHEMA_TABLE} (
          version INTEGER PRIMARY KEY,
          name TEXT NOT NULL,
          applied TEXT NOT NULL
        )
        """
    )


def discover_migrations(directory: Path | None = None) -> tuple[Migration, ...]:
    """Read and validate migration files, ordered by version."""
    directory = Path(directory or MIGRATIONS_DIR)
    migrations: dict[int, Migration] = {}
    for path in sorted(directory.glob("*.sql")):
        match = _MIGRATION_FILE_RE.match(path.name)
        if not match:
            raise DatabaseError(
                f"bad migration filename: {path.name} (expected NNN_name.sql)"
            )
        version = int(match.group(1))
        if version in migrations:
            raise DatabaseError(
                f"duplicate migration version {version}: "
                f"{migrations[version].path.name} and {path.name}"
            )
        migrations[version] = Migration(
            version=version,
            name=match.group(2),
            sql=path.read_text(encoding="utf-8"),
            path=path,
        )
    if not migrations:
        raise DatabaseError(f"no migrations found in {directory}")
    return tuple(migrations[version] for version in sorted(migrations))


def current_version(conn: sqlite3.Connection) -> int:
    row = conn.execute(f"SELECT COALESCE(MAX(version), 0) FROM {SCHEMA_TABLE}").fetchone()
    return int(row[0])


def pending_migrations(
    conn: sqlite3.Connection, *, migrations_dir: Path | None = None
) -> tuple[Migration, ...]:
    """Migrations this database has not applied yet.

    Fails if the file was built by a newer Alice than this code.
    """
    migrations = discover_migrations(migrations_dir)
    recorded = _recorded_versions(conn)

    known = {migration.version for migration in migrations}
    newer = sorted(recorded - known)
    if newer:
        raise DatabaseError(
            f"database schema v{max(newer)} is newer than this build "
            f"(v{max(known)}); update Alice before running it"
        )
    return tuple(m for m in migrations if m.version not in recorded)


def database_status(
    db_path: Path | str, *, migrations_dir: Path | None = None
) -> DatabaseStatus:
    """Describe the database without changing it."""
    path = Path(db_path)
    if not path.is_file():
        return DatabaseStatus(initialized=False, version=None, pending=(), tables=())

    conn = connect(path, create=False)
    try:
        if not _table_exists(conn, SCHEMA_TABLE):
            return DatabaseStatus(
                initialized=False,
                version=None,
                pending=(),
                tables=table_names(conn),
            )
        return DatabaseStatus(
            initialized=True,
            version=current_version(conn),
            pending=tuple(m.version for m in pending_migrations(conn, migrations_dir=migrations_dir)),
            tables=table_names(conn),
        )
    finally:
        conn.close()


def table_names(
    conn: sqlite3.Connection, *, include_internal: bool = False
) -> tuple[str, ...]:
    rows = conn.execute(
        """
        SELECT name FROM sqlite_master
        WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
        ORDER BY name
        """
    ).fetchall()
    names = [str(row[0]) for row in rows]
    if not include_internal:
        names = [
            name
            for name in names
            if name != SCHEMA_TABLE and "_fts_" not in name and not name.endswith("_fts")
        ]
    return tuple(names)


def count_rows(conn: sqlite3.Connection, table: str) -> int:
    if not _SAFE_IDENTIFIER_RE.match(table):
        raise DatabaseError(f"invalid table name: {table!r}")
    if table not in table_names(conn, include_internal=True):
        raise DatabaseError(f"unknown table: {table}")
    row = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()
    return int(row[0])


def _recorded_versions(conn: sqlite3.Connection) -> set[int]:
    rows = conn.execute(f"SELECT version FROM {SCHEMA_TABLE}").fetchall()
    return {int(row[0]) for row in rows}


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone()
    return row is not None


def _is_foreign_database(conn: sqlite3.Connection) -> bool:
    if _recorded_versions(conn):
        return False
    rows = conn.execute(
        """
        SELECT name FROM sqlite_master
        WHERE type = 'table' AND name NOT LIKE 'sqlite_%' AND name != ?
        """,
        (SCHEMA_TABLE,),
    ).fetchall()
    return bool(rows)


def _apply_migration(conn: sqlite3.Connection, migration: Migration) -> None:
    script = (
        "BEGIN;\n"
        f"{migration.sql}\n"
        f"INSERT INTO {SCHEMA_TABLE} (version, name, applied) "
        f"VALUES ({migration.version}, '{migration.name}', '{utc_now()}');\n"
        "COMMIT;"
    )
    try:
        conn.executescript(script)
    except sqlite3.Error as exc:
        if conn.in_transaction:
            conn.rollback()
        raise DatabaseError(
            f"migration {migration.version:03d}_{migration.name} failed: {exc}"
        ) from exc
