# Alice architecture

Alice is a modular monolith: one process, one SQLite database, one config file,
but hard module boundaries. Each module is developed and tested against
contracts and fakes, without importing a sibling's internals. `app` is the only
place that knows how the modules fit together.

This is deliberate. Microservices and separate repositories would add network
failures, version skew, and deployment work to a single-user background agent.
The modularity we need is *ownership and testability*, not process separation.
If a module later earns its own distribution, the dependency rules make that a
packaging change, not a rewrite.

## Module map

```
                    +-----------------------+
                    |        contracts      |   types + protocols, no deps
                    +-----------+-----------+
                                |
        +-----------------------+-----------------------+
        |                       |                       |
     +--v---+              +----v----+             +----v----+
     | core |              | plugins |             | testing |  (dev only)
     +--+---+              +----+----+             +---------+
        |
   +----+------+--------+---------+---------+---------+
   |           |        |         |         |         |
 +-v-----+ +---v----+ +-v-----+ +-v-----+ +-v-----+ +-v------+
 | inputs| | memory | | models| | brain | |outputs| | (future|
 +-------+ +--------+ +-------+ +-------+ +-------+ |  screen)|
        \       |          |         |         /
         \      |          |         |        /
          +-----+----------+---------+-------+
                          |
                       +--v---+
                       | app  |   composition root + CLI + scheduler
                       +------+
```

### Dependency rule

Every module may import `contracts` and `core`. No module may import another
module's internals. `app` may import every module and is the only place that
constructs cross-module services. `contracts` imports nothing from Alice.
`core` may import `contracts`.

Enforced by `tests/test_architecture.py`, which parses imports. If a module
needs something from a sibling, the answer is a protocol in `contracts` and an
implementation injected by `app` — not a direct import.

## Directory layout

```
src/alice/
  __init__.py
  __main__.py              entry point for python -m and PyInstaller
  contracts/               pure interfaces and shared value types
    __init__.py
    models.py              Message, Completion
    memory.py              MemoryItem, MemoryStore, Recall
    outputs.py             Suggestion, SuggestionSink, Notifier
    plugins.py             PluginManifest, Plugin
  core/                    infrastructure shared by every module
    config.py              YAML loading, validation, shared config types
    db.py                  connection, pragmas, migrations
    events.py              ledger writes, Event/RawEvent, cursors, raw store
    backup.py              DPAPI-encrypted snapshot, restore, pruning
    actions.py             explicit allowlisted fix execution + audit rows
    startup.py             logon Scheduled Task install/status/remove
    logging.py             rotating file log
    paths.py               runtime data location, profiles, migration
    migrations/            SQL, one file per schema version
  inputs/                  everything that observes the outside world
    __init__.py            sensor factory + poll loop
    gate.py                normalization, dedupe, salience
    redact.py              PII and secret masking
    files.py               read-only, root-confined chat attachments
    sensors/
      base.py              Sensor interface
      common.py            cursor encoding, head hashes
      logfile.py           rotation-safe log tailer
      logscan.py           bounded whole-drive discovery + cached scan
      message_stub.py      local message file
  memory/                  what Alice remembers
    __init__.py
    config.py              core-memory file, caps, TTL, embedding model
    core_file.py           editable memory/core.json
    hard.py                approved facts and proposals
    fast.py                per-task scratchpad and TTL
    archive.py             FTS5 + sqlite-vec recall
    embeddings.py          lazy CPU multilingual embeddings
    service.py             memory facade
    seed.py                identity/rules/facts templates
  models/                  how Alice talks to models
    __init__.py            Router + build_router
    base.py                Provider interface
    router.py              tier routing, retry, pricing
    anthropic.py           direct API adapter
    opencode.py            OpenCode CLI adapter
    fake.py                deterministic provider
    budget.py              usage log, caps, kill switch
  brain/                   what Alice decides
    __init__.py
    config.py              triage and consolidation timing
    json_output.py         JSON object extraction
    prompts.py             prompts for triage, reasoning, consolidation
    triage.py              Tier 1 batching and decisions
    reason.py              Tier 2 escalation reasoning and structured suggestions
    consolidate.py         Tier 3 nightly summaries and proposals
    chat.py                read-only chat over ledger, memory, and suggestions
  outputs/                 how Alice tells Jo
    __init__.py
    config.py              notify mode, quiet hours
    queue.py               suggestion queue and throttling
    inbox.py               in-app notification read state
    digest.py              daily markdown
    report.py              weekly usage/activity markdown
    notify.py              legacy toast adapter, disabled by default
  plugins/                 optional extensions
    __init__.py
    registry.py            explicit built-in registry, no disk loading
  app/                     composition root
    __init__.py
    cli.py                 click commands
    settings.py            composes per-module config
    bootstrap.py           builds and wires services
    dashboard.py           read-only data for the GUI
    gui.py                 Tkinter desktop dashboard
    activity.py            foreground app, title, idle seconds
    accessibility.py       bounded Windows UI Automation context
    ambient.py             calendar/focus/context-switch rules
    calendar.py            local .ics parsing
    monitor.py             screen UI+OCR analysis and suggestions
    roam.py                supervised read-only PC roaming worker
    screen.py              in-memory Windows OCR capture
    system_info.py         CPU/RAM/disk snapshot
    tools.py               read-only tool registry
```

## What goes in each module

### contracts

Shared vocabulary only. Dataclasses, protocols, enums, and errors that cross a
module boundary. No database, config, logging, network, or file access.

- `Message`, `Completion`: model request/response values.
- `MemoryItem`, `Recall`: what `recall()` returns.
- `MemoryStore`: protocol used by `brain` to read/write memory.
- `Suggestion`, `SuggestionSink`: `brain` submits, `outputs` owns.
- `Notifier`: delivery interface.
- `PluginManifest`, `Plugin`: extension contract.

Owns no tables, no files, no config section. Test count target: trivial tests
for value validation only.

### core

Infrastructure and the append-only ledger. If two modules need it and it is not
a protocol, it belongs here.

- Config loader helpers and core-owned settings (paths, retention, backup).
- `Event`, `RawEvent`, ledger writes, cursors, raw payload store.
- Encrypted local backups and restore verification.
- Explicit, user-approved fix execution with an executable allowlist and audit
  rows. No automatic or model-triggered execution.
- Logon Scheduled Task helpers.
- Migrations for the whole database, with table ownership documented below.

Owns tables: `schema_migrations`, `events`, `cursors`, `backup_runs`, `actions`.
Config sections: top level, `data_dir`, `retention`, `backup`, `actions`.
Depends on: `contracts`.

### inputs

Turns the outside world into ledger events. Read-only and model-free.

- Sensor interface and adapters (single log file/dir, bounded whole-drive scan,
  message stub, later email).
- Redaction, normalization, dedupe, salience.
- The poll → redact → gate → ledger loop.

Owns no tables (writes through `core.events`).
Config sections: `sources`, `gate`, `redaction`, `files`.
Depends on: `contracts`, `core`.
Must not: call a model, write files outside `state`, mutate user files. File
attachments are read-only, root-confined, redacted, and never indexed.

### memory

The three-layer memory system.

- `hard`: versioned, user-approved facts; proposals; 2K-token prompt cap.
- `fast`: task scratchpad, TTL, outcome on expiry.
- `archive`: FTS + vectors, summaries, `recall(query, k, since)`.
- Seed templates.

Owns tables: `hard_memory`, `proposals`, `tasks`, `summaries`,
`memory_embeddings`.
Config section: `memory`.
Depends on: `contracts`, `core`.
Must not: write `hard_memory` except through the user-approved path.

### models

Provider adapters, tier routing, accounting.

- `Provider` interface, `anthropic`, `opencode`, `fake`.
- Router: tier → model, pricing, one retry, usage logging.
- Budget guard and kill switch.

Owns table: `usage`.
Config sections: `models`, `budget`.
Depends on: `contracts`, `core`.
Must not: know what a triage decision is, or write suggestions.

### brain

Policy and reasoning: the only module that decides what matters.

- Prompt assembly (static prefix, hard memory, recall, observed data).
- Tier 1 triage, Tier 2 reasoning, Tier 3 consolidation.
- Structured output validation.

Owns table: `triage_batches`.
Config section: `triage`.
Depends on: `contracts`, `core`.
Must not: execute an action, write `hard_memory`, or call a provider directly —
it receives a `ModelRouter` protocol.

### outputs

Everything Jo sees.

- Suggestion queue, quiet hours, priority threshold, and cluster merges.
- In-app notification inbox with read state for the GUI bell, banner, and Pings
  page. Windows toasts are disabled.
- Daily digest markdown, no summary toast.
- Budget alerts supplied by an injected `BudgetSnapshot`; no import of `models`.

Owns tables: `suggestions`, `notifications`.
Config section: `notify`.
Depends on: `contracts`, `core`.
Must not: decide that something is important; it only delivers.

### plugins

Optional, explicitly registered extensions. Built-ins only by default; no
scanning arbitrary folders for code.

- Plugin manifest, capability declaration (`sensor`, `notifier`, `job`).
- Registry with duplicate detection and capability filtering.
- Host passes a narrow context; plugins do not receive the database, config,
  secrets, or the filesystem.
- Plugins are trusted in-process Python. There is no sandbox. Phase 1 exposes
  no action capability, so a plugin cannot act on Jo's behalf.

Owns no tables.
Config section: `plugins` (enabled list).
Depends on: `contracts`, `core`.

### app

Composition root and process lifecycle.

- Click CLI (`init`, `run`, `status`, `pause`, `resume`).
- Build config → open DB → construct modules → wire protocols.
- Scheduler for polls, triage timer, nightly consolidation.
- Future Windows Task Scheduler/service entry.

Owns no tables.
Depends on: every module.
Must not: contain domain logic. If a decision needs a test of its own, it
belongs in a module.

The desktop GUI lives here because it is presentation and composition, not a
decision-maker. It reads the database, can pause/resume calls, approve/reject
proposals, and write a digest. It never senses, reasons, or acts on the user's
behalf.

## Data ownership

One writer per table. Other modules read through a service, never through raw
SQL.

| Table | Owner | Written by | Read via |
| --- | --- | --- | --- |
| `schema_migrations` | core | core | core |
| `events` | core | inputs (through core) | core |
| `cursors` | core | inputs (through core) | inputs |
| `hard_memory` | memory | user approval only | `MemoryStore` |
| `proposals` | memory | memory | memory/CLI |
| `tasks` | memory | memory | `MemoryStore` |
| `summaries` | memory | memory (requested by brain) | `MemoryStore` |
| `memory_embeddings` | memory | memory | memory |
| `growth_memories` | brain | growth service | GUI/chat prompt |
| `growth_sessions` | brain | growth service | GUI |
| `triage_batches` | brain | brain | brain |
| `consolidation_runs` | brain | brain | brain/status |
| `chat_messages` | brain | brain chat | GUI/chat |
| `actions` | core | core action runner | GUI/status |
| `tool_calls` | app | read-only tools | GUI audit |
| `ambient_alerts` | app | ambient monitor | GUI |
| `roam_sessions` | app | roam service | GUI/CLI |
| `roam_actions` | app | roam service | CLI |
| `backup_runs` | core | core backup service | status/backup |
| `suggestions` | outputs | outputs; user status/cluster merges | outputs/GUI |
| `notifications` | outputs | queue; GUI read state | GUI |
| `budget_alerts` | models | models | models/status/digest |
| `usage` | models | models | budget/status |

Modules may create their own additional tables only by adding a migration in
`core/migrations`, with the table listed here and in the migration comment.

## Data flow

```
sensor.poll()
   │  RawEvent
   ▼
redact ──► normalize ──► dedupe/salience ──► events (core ledger)
                                                │
                       app timer / event trigger│
                                                ▼
                                         brain.triage ──► models.router
                                                │              │
                                   MemoryStore.recall          │
                                                │              ▼
                                                ▼         Completion
                                      SuggestionSink
                                                │
                                                ▼
                                     outputs.queue ──► digest/toast
```

Raw text never crosses a module boundary after redaction except the local raw
store. Models only ever receive redacted, wrapped text.

## Configuration ownership

One YAML file is parsed by `app/settings.py`; each module owns its parser in
`<module>/config.py` and exposes settings types.

| Section | Owner | Status |
| --- | --- | --- |
| top-level paths, retention, backup, actions | core | done |
| `models`, `budget` | models | done |
| `sources`, `gate`, `redaction`, `files` | inputs | done |
| `triage` | brain | done |
| `consolidation` | brain | done |
| `notify` | outputs | done |
| `memory` | memory | done |
| `plugins` | plugins | done |
| `tools` | app | done |
| `growth` | brain | done |
| `ambient` | app | done |
| `monitor` | app | done |
| `roam` | app | done |

## Plugins: policy

1. Built-ins are registered explicitly by `app`; nothing is discovered from the
   filesystem by default.
2. A plugin declares capabilities. The host only calls capabilities it asked
   for.
3. The context object exposes registration functions, not services. A sensor
   plugin cannot reach the database or model router.
4. Failures are contained: a plugin that raises during `start()` is logged and
   skipped; it cannot stop the host.
5. Third-party plugin loading, if added, is opt-in per directory with a warning.
   No action capability in Phase 1.

## Testing a module in isolation

- Fakes live beside the interface they implement (`models.fake`) or in `tests/`.
- Module tests import only that module, `contracts`, `core`, and fakes.
- Every module must be exercisable without a model, without a network, and
  without another module.

```powershell
# from the repo root
.\.venv\Scripts\python.exe -m pytest tests\test_inputs_*.py
.\.venv\Scripts\python.exe -m pytest tests\test_memory_*.py
.\.venv\Scripts\python.exe -m pytest tests\test_models_*.py
```

Test files are named by module prefix so ownership is obvious even while they
share one `tests/` folder.

## Module definition of done

A module is "separate" when all of these are true:

1. It has a README stating purpose, public API, dependencies, config, tables.
2. Its public surface is exported from `__init__.py`; everything else is
   private.
3. Its tests import no sibling module.
4. It has a fake or in-memory implementation of every protocol it consumes.
5. `tests/test_architecture.py` passes for its dependency edges.
6. It can be constructed by `app` without a cycle.
7. It fails loudly on bad config and never writes another module's tables.

## Migration status

| Phase | State |
| --- | --- |
| 1. Move code into module packages, add contracts, plugins, app wiring, boundary test | done |
| 2. Split config parsing per module | done |
| 3. M3 memory implementation inside `memory/` | done |
| 4. M4 triage inside `brain/`, `run --dry-run` | done |
| 5. M5 reasoning and `outputs` queue | done |
| 6. M6 consolidation and read-only GUI | done |
| 7. M7 hardening: startup, crash proof, encrypted backup, report, CI | done |
| 8. Optional extraction of a module into its own distribution | only if needed |

The migration is deliberately mechanical first. Behavior must not change while
the boundaries move; the 350-test suite is the safety net.
