# core

Infrastructure and the append-only ledger. Every module may import `core`;
`core` imports only `contracts`.

- `config.py`: shared parser helpers, `ConfigError`, `CoreConfig`, `Retention`.
- `db.py`: SQLite connection, pragmas, migrations.
- `events.py`: `RawEvent`, `Event`, ledger, cursors, raw payload store.
- `logging.py`: rotating file log.
- `paths.py`: runtime data location, profiles, legacy migration.
- `migrations/`: schema, one file per version.

Owns tables: `schema_migrations`, `events`, `cursors`.
Owns config: top-level paths and `retention`.

Rules:

- No model calls, no sensors, no suggestions.
- Migrations are append-only; never edit an applied file.
- Logs hold metadata only, never content.

Tests: `pytest tests/test_core_db.py tests/test_core_paths.py tests/test_core_logging.py`.
