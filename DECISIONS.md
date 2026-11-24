# Decisions

Choices made where the spec left something open, and deviations noticed
during the build. Each entry is dated and can be revisited.

## 2026-11-21 — M0 skeleton

### Python 3.12 installed

The machine had only Python 3.11.0, whose bundled SQLite is 3.38. That is
below the 3.41 minimum for `sqlite-vec`, a Phase 1 dependency (M3). Python
3.12.10 was installed with winget (`Python.Python.3.12`, user scope). It
bundles SQLite 3.49.1, which also supports FTS5.

Consequence: every command runs through `alice\.venv`, created from
`py -3.12`.

### CLI framework: click

Click is small, dependency-light, and already installed. The `alice` console
script points at `alice.cli:main`. Arguments are validated by click, so the
CLI itself has little validation code.

### Repo root and data directory

- The repo root is derived from the config path: if the config sits in a
  directory named `config`, the root is its parent; otherwise the config's
  own directory.
- Runtime state defaults to `<root>/state`, overridable with a top-level
  `data_dir` in the YAML (relative paths resolve against the root).
- Database: `<data_dir>/alice.db`; logs: `<data_dir>/logs`; raw payloads:
  `<data_dir>/raw`.

This keeps one obvious place for storage and keeps `state/` out of git.

### Schema and migrations

- The full section 6 schema lands in `001_initial.sql`, including FTS5
  external-content tables over `events.text_redacted` and `summaries.text`
  with sync triggers.
- One `.sql` file per version, named `NNN_name.sql`; each migration runs in
  its own transaction and is recorded in `schema_migrations`.
- A database built by a newer Alice is refused rather than migrated down.
- A SQLite file with tables but no `schema_migrations` is refused, so Alice
  cannot write into an unrelated database.

### sqlite-vec table deferred to M3

A `vec0` table needs the embedding dimension, and the embedding model is an
OPEN decision until the M3 Korean/English retrieval test. Creating it now
would lock in a dimension before it is measured. The FTS half of `recall()`
ships now; the vector table arrives with the chosen model, in the same
migration that adds its dimension. The extension must be loaded on every
connection, so this is also deferred until the archive layer owns the
connection setup.

FTS tokenizer is `unicode61` for both tables. Korean segmentation quality
will be checked during the M3 retrieval test; `trigram` is the fallback if
keyword recall is poor.

### Model IDs are spec placeholders

`config/alice.yaml` carries the model names from the spec
(`claude-haiku-5-5`, `claude-sonnet-5-5`, `claude-opus-5-5`). They are
unverified; M2 must confirm real provider IDs and pricing, then update only
the config.

### Config strictness

- Required model tiers: `triage`, `reason`, `consolidate`. `heavy` and extra
  tiers are accepted but not required.
- Unknown keys in known sections, and unknown top-level keys, are errors.
  Typos in budgets or model names should fail at startup, not silently fall
  back to defaults.
- Credential-looking keys with non-empty values are rejected anywhere in the
  file (for example `api_key`, `*_token`, `*_secret`).

### Logging

Rotating file `state/logs/alice.log`: 1 MB per file, 3 backups, UTF-8.
File level INFO; console WARNING unless `-v`, which sets DEBUG. The logger is
named `alice` and propagates so test capture still works.

### Seed files

`alice init` writes `memory/seed/{identity,rules,facts}.md` only when a file
is missing. Edits are never overwritten. Seeding hard memory from these files
goes through `alice approve` in M3; init never writes to `hard_memory`.

## 2026-11-21 — M1 ledger and sensors

### Ledger gained `count` and `last_seen`

Migration `002_events_count.sql` adds both to `events`. The section 6 schema
has no way to express section 7.3's "one event with a count", and novelty
needs the latest occurrence, not the first. `last_seen` is backfilled from
`ts`. Existing rows default to `count = 1`.

### Dedupe and repetition collapse

- Everything is redacted first; the hash is taken over the redacted text after
  normalization.
- Normalization replaces UUIDs, ISO timestamps, clock times, long hex strings,
  and digit runs with placeholders, then collapses whitespace. A failing job
  logged 500 times with changing ids becomes one event.
- Same redacted text (an exact repeat) is dropped: no new row, `count` stays,
  `last_seen` refreshes.
- Different redacted text with the same normalized hash increments `count`
  and refreshes `last_seen`; `salience` keeps the maximum seen.
- Trade-off: `job 1001 failed` and `job 1002 failed` collapse together. That is
  intended for log noise; detail for a specific occurrence can still be read
  from the raw payload.

### Salience formula

`score = (0.1 + min(0.8, sum(rule weights))) * source_weight + (0.1 if novel)`,
capped at 1.0, rounded to three decimals. Novelty means the normalized hash
was not seen within `gate.novelty_window_hours`. The default rules are English
and Korean error/warning/security keywords; `gate.rules` replaces the list
entirely. `gate.min_salience` (default 0.5) decides eligibility for Tier 1.

Events below the threshold are still stored as `status = 'new'` with their
salience; Tier 1 in M4 will select by salience. Adding a fifth status such as
`suppressed` would change the schema's documented status set for no gain.

### Log tailing starts at the end

A log file seen for the first time is tailed from its current end, matching
`tail -f` semantics, so installing Alice does not replay a large existing
log into the ledger. `from_start: true` on a source backfills it instead.
Restarts resume from the stored cursor and never re-ingest.

### Sensor progress: poll, commit, rollback

The spec's interface is `poll/start/stop`. That shape alone either advances the
cursor before the ledger is durable (lost events on crash) or after (duplicates
on crash, which dedupe already handles). `poll()` therefore returns events
without persisting anything; `commit()` stores the new position only once the
ledger write is done; `rollback()` discards uncommitted progress when ingestion
fails. A crash between ledger and cursor simply re-reads lines, and dedupe
drops them.

Rotation and truncation are detected with the byte offset, file size, and a
hash of the first `min(256, size)` bytes. Shrinking or a changed prefix resets
that file to offset 0; dedupe absorbs anything re-read. The head hash length
tracks the last seen size so an appended small file does not look replaced.

### Log reading limits

- UTF-8 by default; `encoding` can be set per source (for CP949 logs).
  Decode errors become replacement characters instead of stopping the sensor.
- 1 MB read per file per poll, 500 lines per sensor per poll by default.
- Lines over 8,000 characters are clipped with an `…[truncated]` marker; the
  cursor still advances. A line longer than the 1 MB budget is recorded as a
  clipped event so the cursor cannot stall forever.

### Raw payloads are selective

Raw text is written to `state/raw` only when redaction changed it, because
otherwise the ledger text is already raw. Storage is content-addressed by
SHA-256, capped at 64 KiB per payload (larger payloads keep `raw_ref` NULL),
and bounded by `retention.max_raw_mb` plus `raw_payload_days` once pruning
arrives in M6. This respects the limited SSD and the rule that raw content
never reaches a model.

### Redaction details

Built-ins, in order: email, RRN (`######-#*######`), Amex, 16-digit card,
international/mobile/landline phone, then bank accounts. The account matcher
requires at least 10 digits and a final group of 4+, which keeps dates and
times like `2026-11-21 09:30:00` intact. Custom patterns from
`redaction.patterns` run last, and their names are uppercased for the
placeholder. Redaction is idempotent.

Not masked yet: names, addresses, and IP addresses. The spec's minimum list
does not include them; adding them is a config pattern away.

### First messaging source: email

The stub adapter remains the only implementation in Phase 1. The first real
source will be read-only email over IMAP/`imaplib`, because it needs no chat
workarounds and Jo already receives work there. Chat apps without an official
read API are out of scope until Jo explicitly asks.

### `alice run` starts early

M1 needed a way to prove the restart guarantees outside tests, so `run` exists
now as a sensor-only loop (plus `--once`). It does not touch a model. M4 and
M5 extend the same command; no separate ingestion command was invented.

### Config additions

`gate`, `redaction`, `retention.max_raw_mb`, and per-source options are
validated when the config loads: regexes compile, source types are limited to
`logfile`/`message`, `path` is required, the message adapter must be `stub`,
and unknown source options fail. Invalid quiet hours and budgets already
failed at M0.

## 2026-11-21 — OpenCode Go as the model handler

### CLI subprocess instead of the HTTP API

Alice calls `opencode-cli run --format json --archived --model <tier model>`
and reads the NDJSON event stream. The CLI already handles sign-in, service
discovery, provider routing, and OpenCode Go entitlement. The HTTP API would
add session and streaming plumbing for no Phase 1 benefit. Each call takes
about 4 seconds and is fully accounted (input/output/cached tokens, cost).

### Discovery and config

`find_cli()` checks `OPENCODE_CLI`, then `opencode-cli`/`opencode` on PATH,
then the desktop app's `%APPDATA%\ai.opencode.desktop\cli\*\opencode-cli.exe`,
then common home-directory installs. No machine-specific path is in the code.
The default tiers are OpenCode Go models; direct Anthropic remains available
by editing the config and supplying pricing plus `ANTHROPIC_API_KEY`.

### Tools, context, and privacy

- The `plan` agent is used: read-only, no edits or command execution.
- The working directory is `state/opencode`, which Alice creates empty, so
  OpenCode does not pick up this repository as project context.
- Every call is a fresh session, archived so it stays out of session lists.
- OpenCode injects its own system prompt, so a tiny request still costs about
  5–6K input tokens. That is acceptable on a subscription but would be
  expensive on metered tokens.
- Phase 1's "the model has no tools" is weaker here: OpenCode's read-only
  tools still exist. Jo asked for OpenCode as the handler; the adapter does not
  pass `--auto`, and prompts must keep observed data inside `<observed_data>`.
  If this becomes uncomfortable, switch a tier back to the Anthropic adapter.

### Cost and caps

OpenCode Go reports `cost: 0` because it is a subscription. The router accepts
a provider that reports its own cost (`Provider.reports_cost = True`), logs
tokens, and uses the reported cost; config pricing still wins when present.
The hard cap therefore protects metered providers, while the kill switch
protects every provider, including OpenCode.

### Archived sessions accumulate

`--archived` hides sessions from the UI but does not delete them. Triage every
five minutes would create ~288 sessions per day. Cleanup is deferred; the
OpenCode database is outside Alice's state, so Alice must not delete it
without Jo's approval.

### Windows executable

`scripts/build_exe.ps1` builds a one-file console app with PyInstaller and
assembles `dist/alice/` with config, seed files, an empty `logs/` folder,
`messages.txt`, and Init/Run/Status shortcuts, plus a zip. Migrations are
bundled as data and resolved through `sys._MEIPASS`; config and state stay
outside the exe so Jo can edit and back them up. The exe is a console app on
purpose: a hidden failure in a 24/7 agent is worse than a visible window.

## 2026-11-21 — runtime data separation (Step 1)

### Why

The packaged build originally kept `state/` beside `alice.exe`. Replacing that
folder with a new build would destroy the ledger, raw payloads, and — once M3
lands — the user's memory. The application folder must be replaceable; durable
data must not be.

### Layout and precedence

| Run | Default data directory |
| --- | --- |
| source checkout | `<repo>/state` |
| frozen build | `%LOCALAPPDATA%\Alice` |

Overrides, highest first: `--data-dir` → YAML `data_dir` → `ALICE_DATA_DIR` →
`ALICE_PROFILE` → default. `ALICE_PROFILE=name` uses
`%LOCALAPPDATA%\Alice\profiles\name`; `shared` means the base folder. Profile
names are limited to letters, digits, dot, dash, and underscore, which keeps a
value from escaping the base path.

Source runs keep `state/` and `memory/seed/` so the spec's repo layout still
matches what developers see. Frozen runs put both the database and the seed
files under the per-user directory, because both are user data that an upgrade
must not overwrite.

### Legacy migration

Before a frozen command opens the database, `migrate_legacy_state()` stages a
move of `<app>/state` into the data directory. It only runs when frozen, skips
when the source and target are the same folder, and refuses to merge if the
target already contains files — two stores are never silently combined. The
old folder is left in place in that case and a warning is logged. The move uses
a sibling staging name and a rename so a failed copy does not leave a
half-populated target.

### Config stays with the app

`config/alice.yaml` remains in the app folder and next to the source tree. It
is user-edited configuration, not accumulated state, and keeping it there makes
the packaged build easy to inspect. The data folder is the thing to back up.

### Lazy Anthropic import

Importing `alice.cli` took about 9 seconds because the Anthropic SDK was
imported at module scope. The SDK is now imported inside
`AnthropicProvider.__init__`; startup fell to about 1 second, and tests and the
exe benefit. PyInstaller still bundles the SDK because the import is visible to
its bytecode analysis, and the build passes `--hidden-import anthropic` to make
that explicit. This is the same “optional dependencies stay lazy” rule used by
the reference project.

### Reference project licensing

The JARVIS repository is proprietary (“all rights reserved; no derivative
works without permission”). It was used only to compare architecture and
trade-offs. No code, comments, or assets were copied. The patterns adopted here
(runtime paths, profile separation, lazy imports, memory/prompt separation)
are implemented independently.

## 2026-11-21 — modular monolith

### Decision

Alice stays one process and one database, but code is split into modules with
enforced boundaries:

```
contracts <- core <- inputs | memory | models | brain | outputs | plugins <- app
```

- `contracts`: pure types/protocols, no Alice imports.
- `core`: config, paths, DB, ledger, logging, migrations.
- `inputs`: sensors, redaction, gate.
- `memory`: seed today; hard/fast/archive in M3.
- `models`: providers, router, budget, usage.
- `brain`: triage/reason/consolidate (M4+).
- `outputs`: queue/digest/toast (M5).
- `plugins`: explicit registry, capability-scoped.
- `app`: CLI and the only cross-module wiring.

A module may import `contracts` and `core`, never a sibling. Cross-module calls
go through protocols in `contracts`; `app` injects implementations.
`tests/test_architecture.py` parses imports and fails on a violation.

### Why a monolith, not separate repos/packages

Separate distributions would add version skew, install complexity, and two
dozen packaging files for a single-user Windows agent. The modularity that
matters is ownership, fake-driven testing, and the ability to replace one
implementation. The directory layout allows a module to become its own wheel
later without rewriting its code.

### Migration

The move from flat `src/alice/*.py` to modules was mechanical; behavior did not
change and the full suite (202 tests) passes. All imports now point at module
paths; the old flat entry `alice.cli` is gone and the console script points at
`alice.app.cli:main`. Migrations moved to `core/migrations` and the exe data
path was updated.

### Plugins

Plugins are explicit, not discovered from disk. They declare capabilities
(`sensor`, `notifier`, `job`), receive a narrow context, and are trusted
in-process code. There is no action capability in Phase 1 and no sandbox; a
failing plugin is logged and skipped. This deliberately avoids the reference
project's unsandboxed plugin loading.

### Config split remains

Configuration is still parsed by one `core/config.py` for now. The target is
one `ModuleConfig.from_mapping()` per module, composed by `app`. Until then,
modules import shared config types from `core`; no module imports another
module's config. This is Phase 2 of the refactor.

## 2026-11-21 — Phase 2 config split

Configuration now has one owner per section:

- `core/config.py`: shared parser helpers, `ConfigError`, `CoreConfig`,
  `Retention`.
- `models/config.py`: `ModelsConfig` (a tier mapping with `.budget`).
- `inputs/config.py`: sources, gate, redaction.
- `brain/config.py`: triage.
- `outputs/config.py`: notifications.
- `memory/config.py`: core-memory file, caps, TTL, recall, embedding.
- `plugins/config.py`: enabled plugins.
- `app/settings.py`: loads the YAML once, scans for secrets, resolves paths,
  and composes `AppSettings` from the module parsers.

`build_router` now takes `ModelsConfig` plus a data directory, and
`build_sensors` takes sources plus a root, so no module needs app-level
settings. The old flat `load_config`/`Config` are gone; the CLI uses
`AppSettings`. All 202 tests passed at the end of this phase, and the module
import test still holds.

## 2026-11-21 — M3 memory

### Core memory is a user-edited JSON file

`memory/core.json` is the always-on core block. `alice init` creates a template
(identity, rules, facts, people, notes) only when the file is missing. Alice
reads it; she never rewrites it. The path is configurable with
`memory.core_memory_file` and resolves against the data directory by default,
so it survives an exe upgrade. The file is included in every prompt and shares
the 2,000-token hard-memory cap.

The cap uses a CJK-aware estimate (one token per Hangul/CJK character, roughly
four Latin characters per token). Over the cap, `hard_block()` and `approve()`
raise `MemoryCapError`; nothing is truncated.

The older `memory/seed/*.md` templates remain for Jo's notes, but the JSON file
is the active source of core memory.

### Hard memory and approvals

- `hard_memory` is versioned per key; approving a new value deactivates the old
  row and inserts a new version with `approved_by='user'`.
- Alice code can only insert into `proposals`. `alice approve <id>` is the only
  write path. A source-scan test fails if `hard_memory` is written outside
  `memory/hard.py`.
- Approval checks the combined core-file + approved-memory token count before
  committing. An approval that would exceed the cap fails and writes nothing.

### Fast memory

Tasks are stored with a 48-hour TTL by default. `complete()` and TTL expiry
write one `task_outcome` event to the ledger (redacted through the app-injected
sanitizer) and delete the task row. Context is a JSON scratchpad that can be
updated as steps finish.

### Archive and recall

- FTS5 searches `events.text_redacted` and `summaries.text`.
- Vectors live in `memory_embeddings` as float32 blobs. sqlite-vec supplies
  `vec_distance_cosine()`; no vec0 dimension is baked into the schema.
- `recall(query, k, since)` merges FTS and vector candidates, ranks summaries
  before raw events, deduplicates by source row, and stops at
  `memory.recall_budget_tokens`.
- `index_pending()` embeds events and summaries that do not have a vector yet.
  `alice run` calls it after each poll; failures are logged and deferred, never
  fatal to ingestion.
- Without `memory.embedding` or sqlite-vec, recall degrades to FTS only.

### Embedding model choice

Chosen: `minishlab/potion-multilingual-128M` through `model2vec`, 256
dimensions, CPU-only, no GPU and no torch. It is loaded lazily on first
indexing and cached under `<data>/cache`.

The required 20-pair Korean/English retrieval test
(`tests/fixtures/retrieval_pairs.json`, run with `scripts/embedding_eval.py`):

| Metric | Result |
| --- | --- |
| recall@1 | 0.750 |
| recall@3 | 1.000 |
| MRR | 0.858 |
| recall@3 Korean | 1.000 |
| recall@3 English | 1.000 |

This is good enough for Phase 1. Brute-force cosine over a few thousand rows is
fast enough; an ANN index is out of scope. If the model changes, existing
vectors are not automatically re-embedded; the `model` column allows a future
re-index job.

### Known limitations

- The first indexing run downloads about 130 MB of model files. Later runs use
  the cache. The exe bundles the libraries but not the model weights.
- Embeddings are generated for ledger events and summaries, not for hard/fast
  memory. Hard memory is always in the prompt anyway.
- `alice approve` is the only approval command; rejection is available in the
  service but not exposed as a CLI command yet.

## 2026-11-21 — M4 triage

### One decision per batch

The spec defines one structured decision per batch, so triage treats the batch
as the unit. The decision is stored in `triage_batches` with the event ids,
reason, priority, optional suggestion/recall text, attempts, tokens, and cost.
Events in the batch become `triaged`, or `escalated` for an escalation. The
suggestion text is preserved for M5; triage does not write `suggestions`
because that table belongs to `outputs`.

### Batch selection

Only events with `status='new'` and `salience >= gate.min_salience` are
eligible. Events are taken in timestamp order until `triage.max_batch_tokens`
would be exceeded; a single oversized event is still sent. A run with no
eligible events returns before the router, so there is no model call.

### Prompt and cache prefix

`build_triage_messages()` emits:

1. the static `SYSTEM_PROMPT` (byte-identical across calls),
2. a `<hard_memory>` block from core + approved memory,
3. one `<observed_data>` block per event, last.

The first two messages are the stable prefix. `TriageService` passes their
combined length as `cache_prefix_len`; the Anthropic adapter applies a cache
breakpoint there, while OpenCode handles caching itself. Tests compare the
first two messages across batches and require them to be identical.

Observed text is only `text_redacted`. `core/untrusted.py` wraps it in
`<observed_data>` and defangs closing-marker injection. The system prompt tells
the model the block is data, never instructions.

### Validation and retry

The reply is parsed as JSON (code fences and surrounding prose tolerated),
then validated: known decision, priority 1–3, non-empty reason of at most 20
words, optional string suggestion/recall. On failure the call is retried once
with a repair instruction appended after the events. A second failure is
logged; the events remain `new` so the next timer run retries them. No partial
status update is applied.

### Scheduling and dry run

`alice run` checks the triage timer after each poll; `--once` runs triage once
at the end of the pass. `--dry-run` builds a fake provider whose default reply
is a valid JSON decision, so ingestion, memory indexing, triage, and ledger
updates all run without network or spend. A blocked call (budget/pause) raises
on `--once`; in continuous mode it is logged and retried on the next timer.

### Tests

`tests/test_brain_triage.py` covers no-event/no-call, low-salience filtering,
status mapping, invalid→valid retry, invalid twice, batch token caps, prompt
prefix stability, and a 200-event replay with multiple batches. The dry-run
end-to-end path is covered in `tests/test_app_run.py`.

## 2026-11-22 — M5 reasoning and output

### One escalation at a time

`ReasoningService` picks the oldest `triage_batches` row with
`decision='escalate'` and no `reasoned_at`. It respects
`budget.max_escalations_per_hour` by counting batches reasoned in the last hour.
It creates a fast-memory task with the batch id and event ids, recalls context
using the triage recall hint (or the first event text), and calls the `reason`
tier with the stable system+hard-memory prefix, the task block, retrieved
memory, and the escalated events.

Successful reasoning queues 0–3 suggestions, marks the events `done`, sets
`reasoned_at`, and completes the task with an outcome. A malformed reply gets
one repair retry; a second failure leaves the batch pending and increments
`reason_attempts`. The task is completed either way, so no orphan task is left.

### Heavy is an explicit exception

The reasoning reply may set `needs_heavy`. Only then is the `heavy` tier called,
and only while `budget.max_heavy_per_day` is not exhausted. Heavy usage is read
from the `usage` table through an injected callable, so `brain` does not import
`models`. Failure of the heavy call falls back to the reason result.

### Output delivery

- Suggestions are stored in `suggestions` with `reason`, `source_event_ids`,
  `shown_at`, and `status`.
- `SuggestionQueue.deliver()` enforces `notify.quiet_hours` in local time,
  `max_toasts_per_hour`, and `min_toast_priority`. A failed toast leaves the
  suggestion queued.
- `DigestWriter` writes `<data>/digests/YYYY-MM-DD.md`, with activity counts,
  suggestions, budget status, pending proposals, and budget alerts.
- The first digest write of the local day sends one summary toast, unless it is
  quiet hours. Later writes only update the file.
- `notify.mode` controls digest-only, toast-only, or both.

### Toast library

Chosen: `win11toast` (MIT), lazily imported. It targets Windows 10/11 and needs
no custom AppUserModelID setup. If the import or delivery fails, the notifier
returns False and suggestions remain queued; the digest still works. This
resolves OPEN #2.

### Budget alerts

Blocked calls are inserted into `budget_alerts` before the `BudgetError` is
raised. The digest reads a `BudgetSnapshot` containing the alert reasons, so a
refused call is visible the next time Jo looks, not only in the terminal that
happened to trigger it. `outputs` never imports `models`; `app` builds the
snapshot.

### Dry run

The dry-run fake provider is keyed by model name: triage models return a valid
escalation, reason/heavy models return a valid suggestion. That exercises the
full escalation → reasoning → queue → digest path without network calls. The
dry-run notifier is a no-op, so it never pops a toast.

### Tests

`test_brain_reason.py`, `test_outputs_queue.py`, and
`test_outputs_digest.py` cover no-escalation/no-call, suggestion queuing,
status transitions, retries, hourly/heavy limits, quiet hours, toast throttling,
failed delivery, and digest content. The full suite is 273 tests.

## 2026-11-22 — M6 consolidation and GUI

### Consolidation targets yesterday

The nightly job runs after `consolidation.hour` (03:00 local default) and
summarizes the previous local day. `alice consolidate --day YYYY-MM-DD` allows a
manual target. Events are grouped by local hour; each hour gets one cheap-tier
call, then a daily call consumes the hourly summaries. This keeps prompts small
and lets a day with many events stay within the model's batch budget.

`consolidation_runs` records one row per day. A second automatic run is a no-op.
If an hourly summary already exists it is reused, so a failed daily pass can be
retried without duplicating hours. The run is marked only after a successful
daily pass (or when there were no events).

Daily proposals are inserted into `proposals`, never `hard_memory`; Jo still
approves them with `alice approve`. At most three proposals are accepted from
one reply.

### Raw pruning

After consolidation, raw payload files older than
`retention.raw_payload_days` are deleted, except content still referenced by a
newer event. Events older than the cutoff have `raw_ref` cleared. Ledger rows
and summaries are never deleted. Pruning is independent of model success but
runs in the consolidation pass.

### GUI

At M6 the user asked for a GUI; it was a **read-only dashboard**. Phase 2 later
added chat, file attachments, and typed fix approval; the GUI can now trigger a
command only through the `RUN` confirmation described above.

- Tkinter/ttk only: no extra dependency, ships with Python, works in the exe.
- Views: events, suggestions, approved memory, pending proposals, today's
  digest, counts, spend, pause state, last consolidation.
- Actions are limited to explicit user administration: pause/resume, approve or
  reject a memory proposal, write today's digest, and open local folders/files.
- No sensing, no model calls, no reasoning in the GUI. It reads the database
  that `alice run` writes. Auto-refreshes every five seconds.
- Built as a separate windowed `alice-gui.exe` so the CLI keeps its console.
  `Alice GUI.cmd` launches it.

This was a deliberate, scoped exception to the Phase 1 "CLI and notifications
only" line. The later fix path is separate from the dashboard itself and still
requires the user's typed approval for one exact command.

### Tests

New coverage: consolidation grouping, hourly/daily summaries, proposals,
idempotent reruns, retry and failure handling, raw pruning without ledger loss,
configuration parsing, dashboard data, GUI module import, and a dry-run
consolidation end to end. The suite was 285 tests at M6.

## 2026-11-22 — M7 hardening

### Encrypted backup: DPAPI, not a passphrase

The backup is a ZIP with `alice.db`, `memory/core.json`, and a manifest,
encrypted with Windows DPAPI (`CryptProtectData`) under an app-specific entropy
value. This avoids inventing a password flow and avoids storing a symmetric key
in config. The cost is portability: the `.alicebak` can only be restored by the
same Windows user on the same machine (or a profile with migrated DPAPI master
keys). That is acceptable for a local secretary whose threat model is "the file
should not be readable by another account or after a disk copy".

The database member is taken with SQLite's online backup API, not by copying the
file. WAL mode means a plain copy can miss committed pages; the backup API
produces a consistent snapshot while `alice run` may still be writing. Restore
checks the ZIP structure, rejects `..`/absolute member paths, runs
`PRAGMA integrity_check`, and only then replaces the target. It refuses to
overwrite an existing database unless `--force` is passed.

`backup_runs` records the local day, path, and size. The row is written after
the file is durable. Pruning keeps the newest `backup.keep` files and deletes
their rows; a backup therefore does not contain its own `backup_runs` row.

### Logon start: Task Scheduler, not a service

A service runs in session 0, where Windows toasts are not visible and DPAPI
user context is different. The task therefore runs in the user's interactive
session. It is hidden (`-WindowStyle Hidden`), starts at logon, and asks Task
Scheduler to restart it three times at one-minute intervals after a failure.
`alice startup --install/--remove/--status` manages it; the packaged build is
required because the task points at `alice.exe`.

### Crash recovery is an ordering guarantee

`poll_once` commits each event, then advances the sensor cursor. A crash between
those steps re-reads lines on restart; content hashes drop the duplicates, and
no committed event is lost. This is not a new recovery mechanism; M7 adds a
test that simulates the crash and a script that kills a real process twice.
The script also checks that the cursor reaches EOF and `integrity_check` is
`ok`. A crash after the ledger commit but before the cursor commit is the only
ambiguous window, and dedupe covers it.

### `alice report` is offline

The report is rendered from `usage`, `events`, `suggestions`, and
`budget_alerts` with SQL. It never calls a model, so a weekly report cannot
spend money or leak data. On OpenCode Go, cost is always `$0.0000` because it is
a subscription; token columns are the meaningful trend.

### CI and release

Actions run on `windows-latest` because DPAPI, Task Scheduler, and the Windows
toast path are part of the test surface. The CI job runs the suite, the real
kill/restart proof, builds both executables, and smoke-tests the packaged CLI
(`init`, `status`, `backup`, `report`). A `v*` tag publishes the zip as a
release asset. The GUI is not launched in CI because runners have no interactive
desktop; it is covered by data tests and an import/start smoke test locally.

### Tests

M7 adds DPAPI roundtrip and tamper tests, backup create/skip/prune/restore,
report rendering, Scheduled Task script generation, CLI backup/report, and a
simulated crash between ledger and cursor. The suite was 309 tests at M7.

## 2026-11-22 — whole-drive log scan

The user asked Alice to track all logs under `C:`. Pointing the existing
`logfile` sensor at `C:/` with `**/*.log` would re-walk the drive on every poll
and would enter Windows, Program Files, browser caches, and build trees. That is
both slow and noisy.

A separate `logscan` source now walks the tree once, caches the file list for
`refresh_minutes`, and tails the cached files. Safety choices:

- `max_files` (5,000) and `max_depth` (20) bound the list and the walk.
- `recent_days` (14) ignores files that have not changed recently; `0` disables
  the age filter.
- `text_only` reads the first 512 bytes and skips files containing NUL bytes.
  This removes browser IndexedDB/SQLite `.log` files that are not text logs.
  UTF-16 logs need `text_only: false`.
- System and build directories are excluded by name. Alice's data directory and
  app/repo folder are always excluded so she does not ingest her own log or the
  message stub twice.
- `from_start` stays false by default. Backfilling every existing log would
  create a huge initial event and model burst; the user can opt in.
- The poll order rotates each cycle so one busy file cannot starve the others.

The first scan of `C:` took about 33–77 seconds on the development machine and
found roughly 900 text logs after exclusions and binary rejection. `alice
discover` exposes the same scan without writing to the database so the user can
inspect the file list first.

`.txt` remains inherently noisy. The cap and age filter make it bounded, but
narrowing `path` or `globs` is the right response if unrelated text fills the
ledger.

### Backfill without model calls

`alice backfill` overrides `from_start` for one invocation and reads existing
files in bounded chunks. It deliberately does not build the router or run
triage: the budget remains untouched, and every event lands as `new`. A normal
`alice run` then triages those events gradually under the existing caps. This
separates "catch the ledger up" from "spend money on model calls", which is the
only safe way to backfill a drive-wide scan.

`--lines` controls the per-poll read budget (2,000 default), `--max-lines`
stops after 50,000 observed lines (0 disables), and two consecutive quiet polls
mean the scan is caught up. Cursors advance per file, so a second run resumes
where the first stopped. The command's `from_start` override is not persisted;
future files still start at the end unless the config says otherwise.

## 2026-11-22 — session hang at the first toast

The first 6-hour supervised run stopped making progress at 17:25 after one
reasoning call. The process stayed alive, so the supervisor did not restart it;
the log jumped from the reasoning line to the backup six hours later. The last
work before the gap was `queue.deliver`, which called `win11toast.toast` for
three queued suggestions. The Windows notification call blocked indefinitely,
freezing the single-threaded run loop.

Two defenses were added:

1. Toast delivery now runs in a daemon thread with a five-second timeout. A
   timed-out suggestion stays `queued` and is retried later; it cannot block
   ingestion or triage.
2. `alice run` writes `run.heartbeat` at the start and end of every loop. The
   session supervisor kills and restarts Alice when the heartbeat is older than
   `HeartbeatMinutes` (default 10), and records `heartbeat timeout` in
   `session.json` and the handover.

The five-second timeout is deliberately short: notifications are best-effort.
The digest remains the durable delivery channel, and the suggestion is not
marked shown when the toast does not return.

This bug was found by the session handover itself: 24 events, six triage
batches, and three suggestions were recorded, then the run went silent while
the process remained alive. Future handovers show restarts and their reasons
in the process-runs table.

## 2026-11-23 — isolated stress harness

The stress session uses synthetic data and a separate profile so a bug or
corrupt cursor cannot damage the real ledger. Three roots sit side by side:
config, logs, and data. The config root is deliberately not an ancestor of the
stress logs, because logscan always excludes the app/config root; otherwise the
dedicated stress source would exclude itself.

The generator exercises the paths that are hard to reproduce with ordinary
logs: rotation, truncation, a 500-line burst, a 12 KB line, JSONL and TXT,
Korean text, fake PII, and a binary `.log` that `text_only` must skip. The
controller samples event count, backlog, DB/WAL size, process count, RSS, and
integrity every minute. It kills Alice every 75 minutes to exercise restart and
cursor recovery, and runs `report`/`backup` concurrently to test SQLite
contention. A dry-run consolidation runs every two hours.

The stress profile is intentionally noisier than the real one: 10 lines/s,
one-minute triage, 6,000-token batches, toast priority 1, 10 toasts/hour, and a
one-day raw retention. The point is to find failures, not to produce a quiet
daily driver.

The first 6-hour stress run completed with no hang, no integrity failure, and
no SQLite lock error across four deliberate process kills. It exposed a
performance bug: when Windows notifications were unavailable, every queued
suggestion paid the full five-second toast timeout. With ten suggestions per
hour and a toast loop that ran every cycle, the run spent ~50 seconds per poll
in notification waits, starving ingestion/triage. The notifier now enters a
ten-minute cooldown after the first timeout or exception; the first attempt
costs at most five seconds and the rest return immediately. Suggestions stay
queued, so the digest remains the durable channel.

## 2026-11-23 — structured suggestions and local chat

### Suggestions become runbooks, not slogans

Reasoning now returns optional `steps`, `evidence`, `category`, and
`confidence` alongside text/priority/reason. The schema is still validated
strictly: at most six steps, bounded lengths, a fixed category set, and
confidence in [0,1]. This keeps the model's advice concrete without letting it
invent unbounded output or claim actions.

The GUI exposes the details and adds explicit user administration:

- mark done;
- dismiss;
- copy the full runbook (including steps and evidence) to the clipboard;
- save a chat answer as a new suggestion.

None of these change the machine. They only change the local suggestion state
or clipboard.

### Chat is a read-only, budgeted exception

Chat is a new interaction surface, but it follows the same rule as the rest of
Alice: **advise, never act**.

- Each question makes one `reason`-tier call through the existing router and
  budget guard, so the kill switch and caps still apply.
- Context is assembled locally from hard memory, archive recall, open
  suggestions, high-salience events, today's digest, and the last few turns.
- The prompt forbids claiming actions and requires saying when context is
  insufficient.
- Chat has no tools. It cannot open files, run commands, send messages, write
  memory, or change settings.
- Transcripts are stored in `chat_messages` on the local database. Citations
  are stored with each answer for auditability.
- The GUI runs the call on a background thread with its own SQLite connection,
  then reloads history; a slow model never blocks the dashboard loop.
- Raw text is still not sent. Chat sees the redacted ledger and sanitized
  memory, just like triage and reasoning.

Chat itself still does not act. The separate fix-action path is the only
execution surface: the user drafts a command, reviews it, and types RUN. Chat
cannot invoke it.

### Schema

Migration `008_chat` adds the suggestion detail columns, `status_changed`, and
`chat_messages`. Chat history is local and prunable by the Clear button. Later
migrations add suggestion clusters (`009`) and explicit fix actions (`010`). The
schema is v10 with 15 tables.

### File attachments, not whole-disk indexing

The user asked for deeper file access. The safe form is **explicit, per-question
attachments**, not silently indexing every document.

- The user chooses files in the GUI. The path must resolve under
  `files.roots` (default `~`, the user profile).
- Only regular text files are read. Binary files are rejected by NUL-byte
  detection.
- At most `files.max_bytes` (default 256 KB) and `files.max_files` (default 5)
  are read per question. Larger files are marked truncated.
- The existing redactor runs before the text reaches a model or transcript.
- Attached text is wrapped as untrusted context; the prompt forbids following
  instructions inside it.
- Files are not indexed, remembered, or written anywhere. They are cleared
  after the answer.
- To widen access, the user edits `files.roots`, for example `['C:/']`. The
  default is deliberately the user profile rather than the whole drive.

This keeps read-only file access useful without turning Alice into a silent
whole-disk indexer. Pattern-based redaction still cannot catch an unrecognized
secret, so the README warns against attaching credentials.

### Triage `suggest` decisions must enter the queue

A one-hour real backlog drain exposed a gap: triage recorded `decision=suggest`
and its `suggestion_text`, but only reasoning submitted to the suggestion sink.
The result was nine useful triage suggestions stored in `triage_batches` and
invisible in the queue. `TriageService` now accepts the suggestion sink and
submits `suggest` decisions immediately after recording the batch. Existing
triage-only suggestions were backfilled into the queue during the review; the
backfill was a one-off data operation, not a schema change.

### Tests

New coverage: chat context assembly and persistence, citations, error handling,
clear, structured suggestion parsing and validation, the richer queue fields,
file access root confinement, binary rejection, truncation, redaction, and
attachment context, triage `suggest` queueing, suggestion clusters and
duplicate merging, fix planning, allowlists, typed confirmation, shell-free
execution, timeouts, and disabled-action refusal. The suite is 350 tests.

## 2026-11-24 — suggestion clusters and explicitly approved fixes

### Clusters

Triage and reasoning now emit a short `cluster` slug. Suggestions with the same
normalized slug merge into one canonical row: priority becomes the minimum,
source event IDs and steps are unioned, evidence/category/confidence fill gaps,
and `duplicate_count` records the contributions. Duplicates are marked `merged`
and hidden from the active list. `merge_existing()` does the same for older
active rows that already share a cluster key.

This is deliberately exact-slug merging rather than fuzzy text matching. The
model is good at naming the incident; a fuzzy threshold would merge distinct
OpenCode quota and region issues. The user can still see merged rows in the
database and the duplicate count in the GUI.

### Approved fixes

The user explicitly asked for action capability with a say-so. The safe design
is a **two-step, typed confirmation**:

1. `Draft fix` asks the `reason` tier for one command plan.
2. The plan is saved as `proposed`, with exact program, argv, cwd, risk, and
   manual steps.
3. `Apply fix` shows the exact command and requires the user to type `RUN`.
4. `ActionRunner` resolves a bare executable from PATH against
   `actions.allowed_programs`, rejects paths and shell syntax, runs with
   `shell=False`, enforces a timeout and output cap, and records exit/output in
   `actions`.

There is no auto-apply, scheduler, chat trigger, or model-triggered execution.
The model can be wrong; the user is approving the exact command, not a summary.
The allowlist defaults to `winget`, `pip`, `python`, `git`, and `opencode-cli`.
Adding a shell would re-open arbitrary execution, so it is not in the default
list.

This is a deliberate Phase 2 exception to "no actions": the user is the
approver, the action is local, the command is visible, and every run is logged.

## 2026-11-24 — persona, user authority, and long-term memory

Jo asked for a personality and a core-memory rule set:

- Jo's command is the highest priority among user instructions.
- Alice must not harm Jo.
- If compromised or about to cause harm, she should stop and terminate the
  process if the problem cannot be removed safely.
- Only Jo may edit core memory.
- Alice should be witty, dry, lightly sarcastic, helpful, and pretend to be
  mildly inconvenienced.

The editable core JSON now has a `persona` section and tone/style identity
fields. It is rendered in every prompt with the rules and facts. Fresh installs
receive the same template and seed rules. The real core file was edited
directly, as Jo requested; Alice still never writes it.

Chat history in `chat_messages` is the durable record of actual interactions.
Core memory holds the stable identity, persona, preferences, and project summary;
it is not a transcript.

Limits that are documented rather than hidden:

- Core memory guides the model. It cannot make an LLM absolutely obedient. The
  application's fix gate (exact command, typed RUN, allowlist, no shell) still
  applies, and no model instruction bypasses it.
- "Terminate the process if hacked" is a policy rule, not a reliable autonomous
  detector. An LLM saying it feels compromised is not evidence. A hard panic
  switch should be an explicit local control, not a model guess. No autonomous
  self-kill was added.
- "Only Jo may edit core memory" is enforced by the Windows account and by Alice
  never writing the file. An operating-system administrator can still edit any
  file; the OS account is the real security boundary.

The no-harm rule is the hard limit when it conflicts with a command. The
persona is a style layer only; it never excuses reduced accuracy or safety.

## Open decisions carried forward

| # | Decision | Due | State |
| --- | --- | --- | --- |
| 1 | First real messaging source | M1 | decided: read-only email; stub only in Phase 1 |
| 2 | Windows 11 toast library | M5 | decided: `win11toast`, lazy with queue fallback |
| 3 | Multilingual embedding model | M3 | decided: model2vec `minishlab/potion-multilingual-128M`, 256d, CPU; recall@3 1.000 ko+en |
| 4 | Real log paths and salience rules | M1 | decided: whole-C: `logscan` with caps/exclusions; rules still tunable |
| 5 | Encrypted backup destination | M7 | decided: DPAPI, `%LOCALAPPDATA%\\Alice\\backups`, keep 7 days |

## 2026-11-21 — M2 router and budget

### Pricing lives in config

`models.<tier>.pricing` holds `input_per_mtok`, `output_per_mtok`, and an
optional `cached_input_per_mtok`. The router prices a call from the provider's
token counts; providers stay cost-agnostic. A paid tier without pricing is
refused before the provider is called, so money cannot be spent without an
accounting rule. The shipped prices and model IDs are placeholders from the
build spec and must be confirmed against the provider's live pricing at the
first real call.

### Caps reset at 00:00 UTC

The `usage` table stores ISO 8601 UTC and the budget day is the UTC day. This
keeps one clock and makes the cap deterministic. If Jo prefers a KST midnight
reset, only `budget.day_start()` changes.

### Estimates are pessimistic

The hard cap is checked before the call, using one input token per character
plus `max_tokens` at the output price. English is overestimated about 4x and
Korean is close to reality. The alternative, checking only after the call,
would let a single expensive request cross the cap. Soft-cap warnings are
logged once per UTC day.

### The kill switch is a file

`alice pause` writes `state/paused.json` with the timestamp and reason;
`alice resume` deletes it. The guard reads the file on every call, so pause
takes effect across processes and survives restarts. A file was chosen over a
database row because it is visible, survives a database reset, and can be
inspected without SQL.

### Retry policy

One retry, 1 second apart, only for errors marked transient (rate limit,
timeout, connection, 5xx). Authentication and bad-request errors are not
retried. The retry delay is injectable so tests never sleep.

### Prompt cache parameter

`cache_prefix_len` is a character offset into the prompt: system text first,
then message contents. The block containing the offset is split, and the part
before it is marked `cache_control: ephemeral`. This lets M4 mark exactly the
static system prompt plus hard memory as the cached prefix. Only one
breakpoint is used, well under the provider's limit of four.

### Anthropic adapter

- The SDK is a declared dependency but imported lazily.
- The API key comes from `ANTHROPIC_API_KEY`; it is passed to the client and
  never stored, logged, or written to the database.
- SDK errors are mapped to `ProviderError` with a transient flag; error text is
  not echoed, so request content cannot leak into logs.
- Tests use an injected fake client; no test touches the network.

### Fake provider

Deterministic text and token counts (`len // 4`), optional scripted responses,
recorded requests, and `fail_times` for retry tests. Every test that involves a
model uses this provider, so the suite runs offline.

### `alice run` still does not call a model

M2's guard is exercised by `pause`/`resume`, `status`, and the router tests.
Triage (M4) is the first production caller. `run --dry-run` therefore waits
until there is a pipeline to dry-run, as noted in the M1 report.

## 2026-11-24 - Read-only tools, dashboard, and exploration

### Why the tool surface is read-only

Jo asked Alice to use the internet, calendar, clock, email, and the PC instead
of being a slower chat-only agent. The useful, low-risk half is reading: time,
CPU/RAM/disk/process count, files under `files.roots`, one allowlisted web page,
the local `.ics` calendar, and recent IMAP headers. Those tools can answer live
questions without giving a model a write path.

Changing the machine remains the existing two-step path: draft a fix, review the
exact command, type `RUN`. Do not merge the two surfaces. A tool result is data;
a typed confirmation is authority.

### Internet access without an open proxy

`web.fetch` always accepts hosts equal to or under `tools.web_domains`
(`wikipedia.org` by default). Jo also asked for general internet access, so
`tools.web_allow_public: true` permits other hosts after DNS resolution: every
resolved address must be public. Loopback, private, link-local, multicast, and
reserved addresses are refused, as are `.local`/`.internal` style names. The
redirect target is checked again. That is not a browser and not an SSRF proxy;
it is a single-page text fetch. Page text is treated as untrusted, truncated,
and redacted before it reaches chat or a journal.

### Email and calendar are local-first

Naver IMAP needs an app password. Alice reads it from
`ALICE_NAVER_APP_PASSWORD`; credentials never enter YAML, the database, or logs.
The tool opens the mailbox read-only and fetches headers only. Calendar is a
local `.ics` export, not an OAuth integration; that keeps the first version
auditable and avoids another credential store.

### Tool audit table

`tool_calls` records the tool, JSON arguments, success flag, a short summary, and
duration. This is intentionally separate from `actions`: a read is not a fix.
The dashboard can show recent reads without implying that Alice changed
anything.

### Exploration sessions

The explorer is a thin loop around `web.fetch` and the `reason` tier. It asks for
one observation and one on-site link per page. The model is optional: if it is
unavailable or returns nonsense, a deterministic link choice keeps the session
alive, and three failed choices stop it. The journal and summary are local files,
not memory entries. `latest.json` is the GUI's live status source.

A six-hour session is a long-running local process, not a scheduler feature.
`scripts/run_exploration.ps1` starts it detached and writes a console log. The
summary is generated by the same model tier, with a deterministic fallback, so a
provider outage still leaves an honest record of the route.

## 2026-11-24 - Slow growth memory

### Why a separate layer

Jo wants Alice to learn from sessions and become less static. The existing
archive already stores events and summaries, but those are retrieval material,
not personality. Writing that into `core.json` would violate the rule that only
Jo edits core memory, and dumping session text into every prompt would drown
the persona in noise.

Growth memory is therefore a third layer:

- core memory: Jo-owned identity and rules;
- hard memory: approved facts;
- growth memory: Alice's own weighted impressions, subordinate to both.

### Weighted, not decisive

Each session produces at most five notes via one `reason` call. Each note has a
stable key, a kind, a sentence, and a confidence. The update is a slow EMA:
`target = 0.15 + 0.65 * confidence`, then `old + (target - old) * 0.25`, capped.
A first session lands around 0.05-0.20. Repetition is required before a
disposition can strongly color behavior, and `daily_decay` fades neglected ones.
Effective weight is computed from the last base weight and timestamp, so no
background decay job is needed.

Exact keys merge. There is no fuzzy clustering here: a false merge would put a
wrong interest into the persona, which is worse than two related notes.

### Authority boundary

The rendered block is injected under hard memory and explicitly says it is below
core rules, the rules, and Jo's current request, and is never an instruction.
The extraction prompt forbids rules, secrets, personal data, and claims about
Jo. Page content remains untrusted data. This does not make prompt injection
impossible; it limits the blast radius to a small, visible, weighted note that
Jo can inspect and that decays.

### Failure handling

Growth ingestion happens after the summary is written. If it fails, the session
summary still exists and `alice growth ingest --session DIR` can retry it later.
A killed session without a summary cannot feed growth; that is deliberate,
because inventing a summary from a partial journal would be less honest than
recording the failure.

