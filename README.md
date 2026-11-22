# Project Alice

Alice is a local, read-only secretary agent for Windows. She watches log files
and messages, decides what matters, remembers useful context, and offers
suggestions. She never acts on the computer, never sends raw content to a
model, and never spends money without an explicit budget.

This repository contains the Phase 1 brain: inputs, redaction, an append-only
ledger, a tiered model router, three memory layers, triage, and a Windows
executable. It is a **modular monolith**: one process and one SQLite database,
with strict module boundaries and an import test.

**Status:** M0-M4 complete. M5 reasoning and output is next. 256 tests pass.

---

## Contents

- [What Alice is](#what-alice-is)
- [Current capabilities](#current-capabilities)
- [Architecture](#architecture)
- [Quick start](#quick-start)
- [Commands](#commands)
- [Configuration](#configuration)
- [Memory](#memory)
- [Triage](#triage)
- [Models, OpenCode Go, and cost control](#models-opencode-go-and-cost-control)
- [Data, tables, and privacy](#data-tables-and-privacy)
- [Windows executable](#windows-executable)
- [Testing and evaluation](#testing-and-evaluation)
- [Repository layout](#repository-layout)
- [Milestones](#milestones)
- [Future plans](#future-plans)
- [Known limitations](#known-limitations)
- [Design notes](#design-notes)

---

## What Alice is

Alice is designed around four priorities, in order:

1. **Never leak personal data.**
2. **Never spend money silently.**
3. **Keep the code simple and readable.**
4. **Make every component swappable** — models, providers, sensors, notifiers.

She runs continuously in the background, mostly without AI. Deterministic code
tails logs, masks personal data, hashes and groups events, and computes a
salience score. A cheap model is called only for batches that pass the gate,
and only when there is something new. A stronger tier handles escalations.
Nightly work (next milestone) consolidates the day.

### Phase 1 non-goals

- No screen watching, vision, or camera.
- No actions on Jo's behalf: no sending messages, editing files, or running
  commands.
- No cloud database or remote VM.
- No GUI beyond the CLI and Windows notifications.
- No unsandboxed third-party plugins with action capabilities.

---

## Current capabilities

| Area | Implemented |
| --- | --- |
| Configuration | One YAML file, per-module parsers, secret rejection, unknown-key rejection |
| Runtime data | Per-user profile, `--data-dir`/env/profile overrides, legacy migration |
| Inputs | Rotation-safe log tailer, local message stub, cursors, encoding recovery |
| Privacy | Korean RRN, phone, card, bank account, email, custom regex masks; raw payloads local-only |
| Gate | Normalization, exact dedupe, near-duplicate collapse with count, rule-based salience, novelty |
| Ledger | SQLite WAL, FTS5, append-only events, raw payload references, retention metadata |
| Models | OpenCode Go via the desktop CLI, direct Anthropic adapter, deterministic fake |
| Budget | Usage log, soft/hard daily caps, pre-call estimates, `alice pause`/`resume` |
| Memory | Editable JSON core memory, versioned approved facts, task scratchpad with TTL, FTS + sqlite-vec recall |
| Embeddings | Local CPU multilingual embeddings (`potion-multilingual-128M`, 256d) |
| Triage | Batched Tier 1, strict JSON decisions, one retry, ledger status updates, `--dry-run` |
| CLI | `init`, `run`, `status`, `pause`, `resume`, `approve` |
| Packaging | Single-file `alice.exe`, portable folder, zip, setup shortcuts |

---

## Architecture

```
contracts ← core ← inputs | memory | models | brain | outputs | plugins ← app
```

- **contracts** — pure types and protocols; no Alice imports.
- **core** — config helpers, paths, SQLite, ledger, logging, migrations.
- **inputs** — sensors, redaction, dedupe/salience, ledger writes.
- **memory** — core JSON, hard/fast/archive, embeddings, recall.
- **models** — providers, tier routing, pricing, usage, budget, kill switch.
- **brain** — prompt assembly, triage, reasoning, consolidation.
- **outputs** — suggestion queue, digest, toasts.
- **plugins** — explicit registry, capability-scoped extensions.
- **app** — CLI, settings composition, and wiring. The only module allowed to
  import every other module.

A module may import `contracts` and `core`, never a sibling. Cross-module calls
use protocols and are wired in `app`. `tests/test_architecture.py` parses every
import and fails on a boundary violation.

See [ARCHITECTURE.md](ARCHITECTURE.md) for the full module map, data ownership,
dependency rules, plugin policy, and module definition of done. Decisions and
trade-offs are recorded in [DECISIONS.md](DECISIONS.md).

### Data flow

```
sensor.poll()
   │  RawEvent
   ▼
redact ──► normalize ──► dedupe / salience ──► events (SQLite ledger)
                                                  │
                             timer / new batch    │
                                                  ▼
                                           brain.triage
                                                  │
                                  MemoryStore.hard_block()
                                                  │
                                                  ▼
                                           models.router
                                                  │
                                    validated JSON decision
                                                  │
                        ┌─────────────────────────┴───────────────────┐
                        ▼                                             ▼
              event status updated                        triage_batches (M4)
                        │                                             │
              triaged / escalated                         M5 suggestions/digest
```

Raw logs and messages never reach a model. Only redacted event text, wrapped in
`<observed_data>`, is sent.

---

## Quick start

### Requirements

- Windows 10/11, 64-bit.
- The **OpenCode desktop app**, installed and signed in to OpenCode Go. Alice
  asks the OpenCode CLI to run model calls; no model API key is stored.
- Python 3.12 for source runs, or the packaged `alice.exe`.

### From source

```powershell
cd alice
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\alice.exe init
.\.venv\Scripts\alice.exe status
```

A free end-to-end check that exercises ingestion, memory, and triage with the
fake provider:

```powershell
.\.venv\Scripts\alice.exe run --once --dry-run
```

Then run continuously:

```powershell
.\.venv\Scripts\alice.exe run
```

`run --once` does one poll pass and exits. Continuous mode polls sensors every
30 seconds and checks triage on `triage.interval_minutes`.

---

## Commands

```text
alice init                       Create data dirs, database, migrations, seed files, core memory
alice run [--once] [--dry-run]   Poll inputs, ingest, index memory, and triage
alice status                     Show config, schema, sources, counts, memory, spend, paused state
alice pause [--reason TEXT]      Block every model call until resumed
alice resume                     Allow model calls again
alice approve [ID]               List pending memory proposals, or approve one
```

Global options: `--config PATH` (or `ALICE_CONFIG`), `--data-dir PATH`,
`-v/--verbose`.

`alice approve` is the only command that writes approved hard memory. It always
records `approved_by='user'`.

---

## Configuration

The file is `config/alice.yaml` (or the path given by `--config`). Secrets are
rejected; keys come from the environment or Windows Credential Manager.

```yaml
models:
  triage:      {provider: opencode, model: opencode-go/deepseek-v4.1-flash}
  reason:      {provider: opencode, model: opencode-go/deepseek-v4-pro}
  heavy:       {provider: opencode, model: opencode-go/kimi-k3.1-pro}
  consolidate: {provider: opencode, model: opencode-go/glm-5.3-flash, batch: true}

budget:
  soft_daily_usd: 1.00
  hard_daily_usd: 2.50
  max_escalations_per_hour: 6
  max_heavy_per_day: 3

triage:
  interval_minutes: 5
  max_batch_tokens: 3000

sources:
  - {type: logfile, path: './logs', glob: '*.log', from_start: true}
  - {type: message, adapter: stub, path: './messages.txt'}

memory:
  core_memory_file: './memory/core.json'
  hard_max_tokens: 2000
  task_ttl_hours: 48
  recall_budget_tokens: 1000
  recall_k: 5
  embedding:
    model: minishlab/potion-multilingual-128M
    dimension: 256

gate:
  min_salience: 0.5
  novelty_window_hours: 24
  source_weights: {logfile: 1.0, message: 1.2}

redaction:
  patterns:
    - {name: AWS_KEY, pattern: '\bAKIA[0-9A-Z]{16}\b'}

notify:
  mode: digest+toast
  quiet_hours: '22:00-08:00'
  max_toasts_per_hour: 3

retention:
  raw_payload_days: 14
  max_raw_mb: 50

plugins:
  enabled: []
```

Model names live only in this file. Every config section has one owner parser
under its module; `app/settings.py` composes them.

---

## Memory

Alice has three memory layers plus an editable core file.

### Core memory (user-owned JSON)

`alice init` creates:

```text
source:   <data>/memory/core.json
packaged: %LOCALAPPDATA%\Alice\memory\core.json
```

Edit it by hand. Alice reads it at startup and **never overwrites it**.

```json
{
  "version": 1,
  "identity": { "name": "Alice", "owner": "Jo", "role": "local secretary" },
  "rules": [
    "Suggest, never act.",
    "Never send raw logs or messages to a model."
  ],
  "facts": { "timezone": "Asia/Seoul", "languages": "Korean and English" },
  "people": {},
  "notes": ["Edit this file to change Alice's always-on core memory."]
}
```

The file is always in the prompt and shares the 2,000-token hard-memory cap.
Over the cap, Alice fails loudly instead of truncating. `alice status` shows the
path and approximate token count. The path is configurable with
`memory.core_memory_file`, relative to the data directory.

### Hard memory (things Jo approved)

Hard facts are versioned in SQLite. Alice code can only create proposals;
`alice approve <id>` is the sole write path and records `approved_by='user'`.
Approving a new value deactivates the old version. A test scans the source to
prove no sensor/brain path writes `hard_memory` directly.

### Fast memory (current task)

A JSON scratchpad per task with a 48-hour TTL. Completion or expiry writes one
`task_outcome` event to the ledger and deletes the task.

### Archive (recall)

`recall(query, k, since)` combines:

- FTS5 over redacted events and summaries,
- optional sqlite-vec cosine search over `memory_embeddings`,
- summaries ranked before raw events,
- a 1,000-token result budget.

`alice run` indexes new events and summaries after each poll and expires due
tasks. Embedding failures are logged and deferred; they never stop ingestion.

### Embedding choice

- Model: `minishlab/potion-multilingual-128M` via `model2vec`
- 256 dimensions, CPU-only, no GPU or PyTorch
- Loaded lazily; downloaded once (~130 MB) into `<data>/cache`
- Without an embedding config or sqlite-vec, recall degrades to FTS only

20-pair Korean/English retrieval test (`scripts/embedding_eval.py`):

| Metric | Result |
| --- | --- |
| recall@1 | 0.750 |
| recall@3 | 1.000 |
| MRR | 0.858 |
| recall@3 Korean | 1.000 |
| recall@3 English | 1.000 |

---

## Triage

`alice run` checks the triage timer after each poll. `--once` runs triage once
at the end. Only events with `status='new'` and
`salience >= gate.min_salience` are eligible. Events are batched up to
`triage.max_batch_tokens`; no eligible events means no model call.

The model must return one JSON object:

```json
{
  "decision": "log",
  "priority": 3,
  "reason": "routine event",
  "suggestion_text": null,
  "recall_query": null
}
```

- `decision`: `ignore`, `log`, `suggest`, or `escalate`
- `priority`: 1 (urgent) to 3 (low)
- `reason`: at most 20 words
- `suggestion_text`, `recall_query`: optional strings or null

Validation rules are enforced. A malformed reply is retried once with a repair
instruction; a second failure logs the error and leaves the events `new` for
the next run.

Ledger behavior:

| Decision | Event status |
| --- | --- |
| ignore / log / suggest | `triaged` |
| escalate | `escalated` |

The decision, reason, suggestion text, recall query, attempts, tokens, and
cost are stored in `triage_batches` for M5.

### Prompt and untrusted data

Prompt order is fixed and cache-friendly:

1. static system prompt (byte-identical between calls),
2. `<hard_memory>` block (changes only when core/approved memory changes),
3. `<observed_data>` event block, last.

The first two messages are stable, and their combined length is passed as the
cache prefix to the router. Observed content is wrapped in `<observed_data>`;
attempts to close the wrapper early are defanged. The system prompt states that
observed text is data, never instructions, and that the model has no tools.

`alice run --dry-run` replaces the provider with a fake whose canned reply is a
valid decision. The full ingestion → memory → triage → ledger path runs without
network or spend.

---

## Models, OpenCode Go, and cost control

### Providers

- **opencode** — default; delegates to `opencode-cli run --format json` using the
  signed-in OpenCode Go subscription. The CLI is discovered automatically;
  override with `OPENCODE_CLI`. Calls use the read-only `plan` agent and an
  empty working directory under the data folder.
- **anthropic** — direct Messages API adapter, lazy SDK import, prompt-cache
  breakpoint support. Requires `ANTHROPIC_API_KEY` and per-tier pricing.
- **fake** — deterministic offline provider used by every test and dry run.

### Cost controls

- Every call is written to `usage` with tier, model, tokens, cached tokens,
  cost, and purpose.
- The soft daily cap logs one warning per UTC day.
- The hard daily cap refuses calls **before** they happen, using a pessimistic
  estimate (one input token per character).
- `alice pause` writes `paused.json` and blocks every call until `resume`.
- Paid providers require `pricing`; unpriced paid calls are refused.
- OpenCode Go reports `$0` because it is a subscription; token usage is still
  logged. The kill switch still blocks it.

---

## Data, tables, and privacy

Runtime data is kept outside the replaceable app folder.

| Run | Default data directory |
| --- | --- |
| Source checkout | `<repo>/state` |
| Built exe | `%LOCALAPPDATA%\Alice` |
| `--data-dir PATH` | that path |
| `data_dir:` in YAML | that path |
| `ALICE_DATA_DIR` | that path |
| `ALICE_PROFILE=name` | `%LOCALAPPDATA%\Alice\profiles\name` |

Precedence: `--data-dir` → YAML → `ALICE_DATA_DIR` → `ALICE_PROFILE` → default.
A frozen build moves a legacy `<app>/state` folder into the per-user directory
before opening the database. It never merges two non-empty stores; it warns and
leaves the old folder in place instead.

The data folder holds `alice.db`, `logs/`, `raw/`, `memory/core.json`,
`memory/seed/`, `cache/`, `opencode/`, and `paused.json`.

### Table ownership

| Table | Owner | Written by |
| --- | --- | --- |
| `schema_migrations` | core | core |
| `events` | core | inputs through core |
| `cursors` | core | inputs through core |
| `hard_memory` | memory | user approval only |
| `proposals` | memory | memory |
| `tasks` | memory | memory |
| `summaries` | memory | memory |
| `memory_embeddings` | memory | memory |
| `triage_batches` | brain | brain |
| `suggestions` | outputs | outputs (M5) |
| `usage` | models | models |

### Privacy rules

- Redaction runs before hashing, storage, or any model call.
- Raw text is kept locally only when redaction changed it; it is never sent.
- The ledger stores redacted text and content hashes.
- Observed data is wrapped as untrusted and cannot close its wrapper early.
- Logs contain metadata, not content.
- The model has no tools and its output is parsed, never executed.
- Hard memory is user-approved; Alice can only propose.
- Personal data is masked in task outcomes before they enter the archive.

---

## Windows executable

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build_exe.ps1
```

Produces:

```text
dist/alice/alice.exe        single-file console app (~42 MB)
dist/alice/config/          editable config
dist/alice/logs/            drop *.log files here
dist/alice/messages.txt     one message per line
dist/alice/*.cmd            Init / Run / Status / Data-folder shortcuts
dist/alice-windows-x64.zip  the whole folder
```

The exe bundles Python, sqlite-vec, and model2vec. It does **not** bundle the
embedding weights; the first indexing run downloads them. Use
`Run Alice.cmd` for init + continuous run, `Alice status.cmd` for a status
screen, and `Data folder.cmd` to open `%LOCALAPPDATA%\Alice`.

---

## Testing and evaluation

```powershell
.\.venv\Scripts\python.exe -m pytest
```

256 tests cover:

- config validation, secret rejection, data-dir precedence, profiles, migration;
- database migrations, WAL, FTS sync, table safety;
- log rotation, partial lines, truncation, encoding errors, cursor recovery;
- redaction of Korean and English fixtures;
- dedupe, collapse counts, salience, raw payload policy;
- router tiers, pricing, retries, budgets, pause, PII scan of outgoing payloads;
- core memory JSON, hard memory approvals and write protection, TTL tasks;
- FTS and sqlite-vec recall, summaries-first ranking, token budgets;
- triage decisions, retries, batching, 200-event replay, prompt-prefix stability;
- CLI and dry-run end-to-end;
- module import boundaries and plugin registry.

All tests run offline with temporary directories. No provider is called and no
embedding model is downloaded; memory tests inject a deterministic embedder.

The real embedding evaluation is manual:

```powershell
.\.venv\Scripts\python.exe scripts\embedding_eval.py
```

---

## Repository layout

```text
alice/
  ARCHITECTURE.md          module map, dependency rules, data ownership
  DECISIONS.md             choices and trade-offs
  README.md
  config/alice.yaml
  packaging/               config, shortcuts, README for the exe
  scripts/
    build_exe.ps1          PyInstaller build + distribution folder/zip
    embedding_eval.py      Korean/English retrieval evaluation
  src/alice/
    contracts/             value types and protocols
    core/                  config helpers, db, ledger, paths, logging, migrations
    inputs/                sensors, redaction, gate
    memory/                core JSON, hard/fast/archive, embeddings, recall
    models/                providers, router, budget, usage
    brain/                 prompts, triage, future reasoning/consolidation
    outputs/               suggestion queue/digest/toasts (M5)
    plugins/               explicit registry and capability policy
    app/                   CLI, settings, composition root
  tests/                   offline tests and fixtures
```

Each module has its own `README.md` describing its API, config, tables, and
test command.

---

## Milestones

| # | Milestone | State |
| --- | --- | --- |
| M0 | Skeleton: repo, config, DB + migrations, logging, CLI `init`/`status` | done |
| M1 | Ledger and sensors: interface, log tailer, redaction, dedupe, salience | done |
| M2 | Router and budget: providers, usage log, caps, `pause` | done |
| M3 | Memory: core JSON, hard/fast/archive, embeddings, `approve` | done |
| M4 | Triage: batches, prompt prefix, validated decisions, dry run | done |
| M5 | Reasoning and output: escalations, suggestions, throttle, digest, toasts | next |
| M6 | Consolidation: nightly summaries, proposals, raw pruning | planned |
| M7 | Hardening: logon start, crash recovery, encrypted backup, cost report | planned |

---

## Future plans

### M5 — Reasoning and output (next)

- Turn `escalate` triage batches into fast-memory tasks.
- Call Tier 2 (`reason`) with recalled context from `recall()`.
- Produce suggestions with source event IDs; never act.
- Add the `outputs` queue: priority threshold, quiet hours, max toasts/hour.
- Write a daily markdown digest plus one summary toast.
- Implement `alice report` for weekly cost review.
- Add a budget alert to the digest so blocked calls are never silent.

### M6 — Consolidation

- Nightly job: roll events into hourly and daily summaries.
- Cheapest model tier, batched, using summaries not raw logs.
- Create hard-memory proposals when stable facts or recurring patterns appear.
- Prune raw payloads past the retention window without losing ledger rows.
- Delete embeddings for pruned events.

### M7 — Hardening

- Start at logon with Task Scheduler (service later).
- Crash recovery proof: kill mid-run, restart, lose nothing, duplicate nothing.
- Encrypted nightly backup with a tested restore.
- `alice report` weekly cost report and usage trends.
- CI: tests, exe build, smoke test, release artifact.

### After Phase 1 is stable

- Read-only email source over IMAP as the first real message adapter.
- Local model option (Ollama/llama.cpp) behind the existing provider interface.
- Better Korean/English retrieval and a re-index job when the embedding model
  changes.
- Optional third-party plugins with explicit capabilities and no action access.
- A small read-only tray/status UI if it remains useful and does not expand the
  attack surface.
- Only then consider any action-taking, with a UI-issued confirmation gate and
  per-action permissions. Screen watching and vision remain out of scope until
  they are explicitly requested and designed.

---

## Known limitations

- OpenCode adds its own system prompt, so a small request still costs roughly
  5–7K input tokens. This is fine on the OpenCode Go subscription but would be
  expensive on metered tokens.
- The embedding model is downloaded on first indexing; it is not bundled into
  the exe.
- Recall uses brute-force cosine over stored embeddings. That is fast enough
  for Phase 1 scale; an ANN index is not implemented.
- Summaries are ranked before events to honor the spec, which can prefer a
  broad summary over a very specific event.
- `alice approve` has no CLI rejection command yet; rejection exists in the
  memory service.
- `memory/seed/*.md` remain as templates; the active core memory is
  `memory/core.json`.
- Triage decisions are stored, but suggestions are not delivered until M5.
- No CI or license file is present yet.

---

## Design notes

- [ARCHITECTURE.md](ARCHITECTURE.md) — module boundaries, protocols, data flow,
  table ownership, plugin policy, and how to develop a module alone.
- [DECISIONS.md](DECISIONS.md) — every non-obvious choice, including OpenCode
  integration, memory caps, embeddings, prompt caching, and runtime data paths.

The JARVIS repository was reviewed only as an architectural reference. It is
proprietary and no code, comments, or assets were copied; the patterns adopted
here were implemented independently.
