# core

Infrastructure and the append-only ledger. Every module may import `core`;
`core` imports only `contracts`.

- `config.py`: shared parser helpers, `ConfigError`, `CoreConfig`, `Retention`,
  `Backup`.
- `db.py`: SQLite connection, pragmas, migrations.
- `events.py`: `RawEvent`, `Event`, ledger, cursors, raw payload store.
- `backup.py`: consistent SQLite snapshot, DPAPI encryption, pruning, verified
  restore.
- `actions.py`: explicit, allowlisted, shell-free fix execution with typed
  confirmation and audit rows.
- `startup.py`: hidden logon Scheduled Task install/status/remove.
- `logging.py`: rotating file log.
- `paths.py`: runtime data location, profiles, legacy migration.
- `migrations/`: schema, one file per version.

Owns tables: `schema_migrations`, `events`, `cursors`, `backup_runs`, `actions`.
Owns config: top-level paths, `retention`, `backup`, and `actions`.

Rules:

- No model calls, no sensors, no suggestions.
- Actions never run automatically: typed confirmation, allowlist, no shell.
- Migrations are append-only; never edit an applied file.
- Logs hold metadata only, never content.
- Backups contain the database and core memory; they are encrypted for the
  current Windows user and are never uploaded.

Tests: `pytest tests/test_core_*.py`.
