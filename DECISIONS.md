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

## Open decisions carried forward

| # | Decision | Due | State |
| --- | --- | --- | --- |
| 1 | First real messaging source | M1 | decided: read-only email; stub only in Phase 1 |
| 2 | Windows 11 toast library | M5 | open |
| 3 | Multilingual embedding model | M3 | decided: model2vec `minishlab/potion-multilingual-128M`, 256d, CPU; recall@3 1.000 ko+en |
| 4 | Real log paths and salience rules | M1 | paths open (Jo to provide); default rules shipped |
| 5 | Encrypted backup destination | M7 | open |

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

