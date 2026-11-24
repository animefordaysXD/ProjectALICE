# Project Alice

Alice is a local secretary agent for Windows. She watches log files and
messages, decides what matters, remembers useful context, and offers
suggestions. By default she does not act. A fix can run only after you draft it,
review the exact command, and type **RUN** in the GUI. She never sends raw
content to a model and never spends money without an explicit budget.

This repository contains the Phase 1 brain: inputs, redaction, an append-only
ledger, a tiered model router, three memory layers, triage, and a Windows
executable. It is a **modular monolith**: one process and one SQLite database,
with strict module boundaries and an import test.

**Status:** Phase 1 complete. Phase 2 adds structured suggestions, local chat with read-only PC/web/calendar/email tools, read-only file attachments, clustering, a hardware/usage dashboard, autonomous Wikipedia exploration sessions, explicitly approved fix actions, a slow-weighted growth memory that learns from exploration sessions, passive local calendar/activity monitoring, local screen OCR with suggestion pings, and an in-app ping inbox. 397 tests pass.

---

## Contents

- [What Alice is](#what-alice-is)
- [Current capabilities](#current-capabilities)
- [Architecture](#architecture)
- [Quick start](#quick-start)
- [Commands](#commands)
- [Configuration](#configuration)
- [Consolidation](#consolidation)
- [Desktop GUI](#desktop-gui)
- [Backups and restore](#backups-and-restore)
- [Weekly report](#weekly-report)
- [Startup, crash recovery, and CI](#startup-crash-recovery-and-ci)
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
Nightly work consolidates the day and then writes an encrypted local backup.

### Phase 1 non-goals

- No screen watching, vision, or camera.
- No **autonomous** actions. A proposed fix executes only after the user reviews
  the exact program/arguments and types **RUN**. There is no scheduled or
  model-triggered execution.
- No cloud database or remote VM.
- No action-capable plugins. The GUI's fix runner is a built-in, allowlisted
  command path, not a general plugin surface.
- No shell interpretation: actions run as argument vectors with
  `shell=False`.

---

## Current capabilities

| Area | Implemented |
| --- | --- |
| Configuration | One YAML file, per-module parsers, secret rejection, unknown-key rejection |
| Runtime data | Per-user profile, `--data-dir`/env/profile overrides, legacy migration |
| Inputs | Rotation-safe log tailer, bounded whole-drive `.log`/`.txt`/`.jsonl` scan, local message stub, cursors |
| Privacy | Korean RRN, phone, card, bank account, email, custom regex masks; raw payloads local-only |
| Gate | Normalization, exact dedupe, near-duplicate collapse with count, rule-based salience, novelty |
| Ledger | SQLite WAL, FTS5, append-only events, raw payload references, retention metadata |
| Models | OpenCode Go via the desktop CLI, direct Anthropic adapter, deterministic fake |
| Budget | Usage log, soft/hard daily caps, pre-call estimates, `alice pause`/`resume` |
| Memory | Editable JSON core memory, versioned approved facts, task scratchpad with TTL, FTS + sqlite-vec recall |
| Embeddings | Local CPU multilingual embeddings (`potion-multilingual-128M`, 256d) |
| Triage | Batched Tier 1, strict JSON decisions, one retry, ledger status updates, `--dry-run` |
| Reasoning | Tier 2 for escalations, fast-memory task, recalled context, optional heavy refinement |
| Output | Structured suggestions with steps, evidence, category, confidence; queue, quiet hours, in-app ping bell, daily digest |
| Consolidation | Nightly hourly/daily summaries, memory proposals, raw-payload pruning |
| GUI | Dark synthwave dashboard, events, suggestion actions, memory approvals, digest, local chat, tools/plugins view, live hardware and token usage, pause/resume, and explicitly approved fixes |
| Chat | Budgeted Q&A with a tool loop over the ledger, memory, digest, files, time, system, web, calendar, and email |
| Tools | Read-only `time.now`, `system.snapshot`, `activity.snapshot`, `file.read`, `file.list`, `web.fetch`, `calendar.upcoming`, `email.recent`; every call audited in `tool_calls` |
| Ambient | Local foreground app/category/idle, calendar lookahead, focus and context-switch feedback, queued through normal suggestions and in-app pings |
| Screen | Opt-in local Windows OCR of the primary monitor; redacted excerpt only, in-memory screenshot, excluded password apps/titles |
| Pings | In-app bell, unread badge, transient banner, optional system beep, read state, digest fallback |
| Roam | Time-boxed, supervised, read-only PC exploration; idle-aware lease, restartable worker, journal, suggestions, growth memory, final summary |
| Files | Read-only chat attachments and directory listing under configured roots, redacted and size-capped; no writes or automatic indexing |
| Exploration | Six-hour-style autonomous read-only Wikipedia sessions with a step journal, JSONL trail, token accounting, and a final summary |
| Growth | Summarized session reflections become weighted dispositions; slow EMA updates, time decay, and a token-capped prompt block beneath core memory |
| Actions | Model-drafted fix command, exact confirmation, executable allowlist, no shell, timeout, local output log |
| Clustering | Stable suggestion clusters, duplicate counts, merged evidence/source events |
| Backup | Nightly DPAPI-encrypted ZIP of the database and core memory; prune + tested restore |
| Report | `alice report` weekly spend, token, tier, model, and activity trends |
| Startup | Hidden logon Scheduled Task with restart-on-failure; `alice startup` |
| Recovery | Crash-safe cursors: restart loses nothing and writes no duplicates |
| CI | Windows GitHub Actions: tests, crash proof, exe build, smoke test, release asset |
| Alerts | Budget refusals recorded and shown in the digest |
| CLI | `init`, `run`, `status`, `discover`, `pause`, `resume`, `approve`, `consolidate`, `backup`, `report`, `startup`, `gui`, `tools`, `explore`, `growth`, `monitor`, `roam` |
| Packaging | Single-file `alice.exe`, `alice-gui.exe`, portable folder, zip, setup shortcuts |

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
- **outputs** — suggestion queue, digest, in-app pings.
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
fake provider. The first whole-drive scan takes roughly 30–80 seconds:

```powershell
.\.venv\Scripts\alice.exe discover --root C:/ --limit 10
.\.venv\Scripts\alice.exe run --once --dry-run
```

With `from_start: false` (the default), existing lines are not replayed, so a
first `--once` may insert nothing until a tracked log changes. Set
`from_start: true` for a deliberate one-time backfill.

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
alice discover [--root C:/] [--max-files N]
                                 Preview which log files a scan would track; no DB writes
alice backfill [--lines N] [--max-lines N]
                                 Read existing logs into the ledger; no model calls
alice pause [--reason TEXT]      Block every model call until resumed
alice resume                     Allow model calls again
alice approve [ID]               List pending memory proposals, or approve one
alice consolidate [--day DATE]   Summarize one day, propose memories, prune raw payloads
alice backup [--list]            Create an encrypted backup, or list local backups
alice backup --restore FILE [--to DIR] [--force]
                                 Verify and restore a backup
alice report [--days N] [--write]
                                 Weekly spend, token, tier, and activity report
alice startup [--install|--remove|--status]
                                 Manage the hidden logon Scheduled Task
alice gui                        Open the desktop dashboard
alice tools                      List the read-only tools
alice tools --run time.now       Run one read-only tool
alice explore --minutes 360      Run a read-only exploration session
alice growth list                Show learned dispositions and weights
alice growth render              Show the prompt block Alice actually sees
alice growth ingest --session DIR  Learn from an existing session
alice monitor --capture          Test local screen OCR once
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
  interval_minutes: 1
  max_batch_tokens: 6000

consolidation:
  hour: 3
  max_events: 500
  max_tokens: 6000

sources:
  - type: logscan
    path: 'C:/'
    globs: ['*.log', '*.txt', '*.jsonl']
    max_files: 5000
    refresh_minutes: 30
    max_depth: 20
    recent_days: 14
    text_only: true
    from_start: false
    exclude_dirs: [Windows, 'Program Files', 'Program Files (x86)', ProgramData,
                   '$Recycle.Bin', 'System Volume Information', Recovery,
                   PerfLogs, MSOCache, Config.Msi, 'Documents and Settings',
                   '$WinREAgent', '$SysReset', .git, node_modules, __pycache__,
                   .venv, venv, site-packages, build, dist, obj]
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

ambient:
  enabled: true
  calendar_lookahead_minutes: 30
  focus_minutes: 90
  idle_minutes: 30
  switch_window_minutes: 30
  switch_threshold: 20
  late_night_hour: 1
  max_alerts_per_hour: 2

tools:
  web_domains: ['wikipedia.org']   # always allowed host suffixes
  web_allow_public: true           # other public hosts; private IPs always refused
  web_max_bytes: 500000
  web_timeout_seconds: 20
  calendar_file: './calendar.ics'  # local .ics under the data directory
  email_enabled: false             # enable after the Naver IMAP app password exists
  email_host: 'imap.naver.com'
  email_port: 993
  email_user_env: 'ALICE_NAVER_USER'
  email_password_env: 'ALICE_NAVER_APP_PASSWORD'
  email_max_messages: 5

roam:
  enabled: true
  idle_minutes: 5
  step_seconds: 300
  max_steps: 500
  memory_seconds: 600
  max_suggestions: 3
  allow_when_active: false

growth:
  enabled: true
  max_notes_per_session: 5
  max_weight: 0.80
  daily_decay: 0.995
  influence_alpha: 0.25
  render_max_tokens: 450
  min_weight: 0.03

actions:
  enabled: true
  allowed_programs: ['winget', 'pip', 'python', 'git', 'opencode-cli']
  timeout_seconds: 120
  max_output_chars: 20000

files:
  roots: ['~']
  max_bytes: 262144
  max_files: 5

notify:
  mode: digest
  quiet_hours: '22:00-08:00'
  max_toasts_per_hour: 0
  min_toast_priority: 2

retention:
  raw_payload_days: 14
  max_raw_mb: 50

backup:
  enabled: true
  dir: backups
  hour: 4
  keep: 7

plugins:
  enabled: []
```

Model names live only in this file. Every config section has one owner parser
under its module; `app/settings.py` composes them.

### Whole-drive log scanning

`type: logscan` walks a tree, caches the matching file list, and tails files.
For `C:/` the first scan takes roughly 30–80 seconds on a normal machine; the
list is then reused for `refresh_minutes`. Before enabling a broad scan, preview
it:

```powershell
alice discover --root C:/ --recent-days 14 --limit 20
```

Safety limits:

- `max_files` stops discovery at 5,000 files, so the event loop stays bounded.
- `max_depth` stops pathological deep trees.
- `recent_days` ignores files that have not changed recently. Use `0` to
  include everything.
- `text_only` skips binary files that merely end in `.log`, such as browser
  IndexedDB or SQLite files. Set it to `false` only if you really need UTF-16
  or other binary-ish logs.
- `exclude_dirs` skips Windows, Program Files, ProgramData, recycle/system
  folders, and build/vendor trees.
- Alice's own data directory and app folder are always excluded, so she does
  not ingest her own log or `messages.txt` twice.
- `from_start` defaults to `false`: existing lines are not replayed, only lines
  written after Alice starts. Set it to `true` only if you accept a large
  one-time backfill.
- Files are polled in a rotating order so one noisy log cannot starve the rest.

The existing `logfile` source remains the right choice for a single known
directory when you do not want a drive-wide scan.

#### One-time backfill

`alice backfill` reads existing lines into the ledger **without calling a
model**, so the budget is untouched:

```powershell
alice backfill --lines 2000 --max-lines 50000
```

- `--lines` is the per-poll read budget (default 2,000).
- `--max-lines` stops the run after that many observed lines (default 50,000);
  `0` means unlimited.
- It stops after two quiet polls or `--max-polls`.
- Cursors advance per file, so running it again resumes rather than restarts.
- Events stay `new`; the normal `alice run` triages them gradually under the
  existing budget caps.
- The command overrides the source's `from_start` only for that invocation, so
  future log files still start at the end unless the config says otherwise.

On this machine the discovered set was about 24 MB across roughly 1,000 files.
A 50,000-line batch keeps the first run bounded; run it again if the summary
says there is still backlog.

---

## Reasoning and output

Escalated batches are picked up by Tier 2, one at a time. Alice creates a
fast-memory task, recalls context from the archive (using the triage recall
hint when present), and calls the `reason` tier with the same stable prefix plus
the task, retrieved memories, and the escalated events. A valid reply is:

```json
{
  "assessment": "1-3 sentences",
  "suggestions": [
    {"text": "one concrete suggestion", "priority": 2, "reason": "why"}
  ],
  "needs_heavy": false
}
```

Zero to three suggestions are allowed. A malformed reply is retried once; a
failure leaves the escalation pending and records the task outcome. Successful
reasoning marks the events `done`, stores the batch as reasoned, and queues the
suggestions.

`needs_heavy` is the only way to use the `heavy` tier. It is refused when
`budget.max_heavy_per_day` is exhausted, and reasoning itself respects
`budget.max_escalations_per_hour`.

### Delivery

- Windows toasts are disabled. Every new suggestion writes a row to
  `notifications` and pings the GUI: bell count, unread badge, transient banner,
  and a system beep.
- The GUI's **Pings** page lists read and unread pings; **Mark all read** clears
  the badge. Notification read state is independent of suggestion status.
- Everything appears in a daily markdown digest at
  `<data>/digests/YYYY-MM-DD.md`.
- Budget refusals are recorded in `budget_alerts` and shown in the digest, so a
  blocked call is never silent.
- If the GUI is closed, pings stay unread in the database and appear when it is
  opened. There is no Windows toast fallback.

## Consolidation

Nightly at `consolidation.hour` (default 03:00 local), Alice summarizes the
previous local day:

1. Events are grouped by local hour.
2. Each hour is summarized by the `consolidate` tier (cheapest), batched and
   token-capped. Existing hourly summaries are reused, so a retry is safe.
3. A daily summary is produced from the hourly summaries.
4. The model may propose up to three stable facts. These are written to
   `proposals`, never to `hard_memory`; `alice approve` is still required.
5. Raw payloads older than `retention.raw_payload_days` are deleted and their
   `raw_ref` cleared. Ledger rows and summaries remain.

`consolidation_runs` records one row per summarized day. A day is not processed
twice unless `alice consolidate --day ...` is run explicitly. `alice run`
checks the consolidation hour and retries a failed daily pass after an hour.

## Desktop GUI

`alice gui`, or `alice-gui.exe` / `Alice GUI.cmd` in the packaged build, opens a
local Tkinter dashboard. It is read-only except for explicit administration and
fix actions you approve:

- **Window:** 1280x820 default (clamped to the screen), centered, resizable, with
  a dark synthwave palette and a compact sidebar.
- **Dashboard:** state, CPU, RAM, disk free, tokens today, tool calls, a
  plain-language "today at a glance" line, latest events, and latest tool calls.
- **Events:** recent redacted events with status and salience.
- **Suggestions:** priority, status, category, confidence, duplicate count,
  text, and reason. Select a row to see its steps and evidence. You can mark it
  done, dismiss it, copy it, save a chat answer, draft a fix, or approve a fix
  by typing RUN.
- **Chat:** ask about events, memory, the digest, the PC, calendar, email, or the
  web. Alice can call read-only tools. You can attach text files. Answers are
  persisted locally and include the context they used.
- **Memory:** core JSON path/token count, approved memory, pending proposals
  with Approve/Reject buttons.
- **Growth:** active learned dispositions with effective weights, evidence
  counts, last-seen times, and the sessions that fed them.
- **Digest:** today's markdown; a button writes/updates it.
- **Pings:** in-app notification list, unread badge, Mark all read, and Open
  selected suggestion. No Windows toasts.
- **Ambient:** current foreground app/category and idle time, the next calendar
  event, screen-monitor status/excerpt, recent ambient alerts, and a shortcut to
  the calendar folder.
- **Roam:** choose a lease length, idle-only or explicit designation, start/stop
  the supervised read-only worker, and inspect active time, steps, last action,
  session folder, and summary.
- **Tools & plugins:** tool readiness, the web allowlist, email/calendar status,
  registered plugins, and the recent tool-call audit.
- **System & usage:** live CPU/memory/disk/uptime/process counters plus total
  input, output, combined tokens, reported cost, usage by tier, and usage by day.
- **Controls:** pause/resume model calls, open the data folder, core memory,
  digest folder, calendar folder, or exploration folder.
- **Refresh:** every five seconds; chat updates when a reply arrives.

The GUI performs no sensing or reasoning in the background. Approving memory,
pausing calls, marking suggestions done/dismissed, and writing a digest are
explicit user actions. Chat is also explicit: it reads local context and makes
budgeted `reason`-tier calls, including at most a few read-only tool calls per
question. It cannot change files, send mail, or run commands.

### Chat

The Chat tab answers questions such as:

- "What are the most urgent open suggestions?"
- "Why did Alice flag the queue at 03:20?"
- "What do you remember about this project?"
- "What time is it, and how much RAM is in use?"
- "What is on my calendar this week?"
- "Use Wikipedia to explain what an Ice Warrior is."
- "What should I check first, and what can you not do?"

Each answer uses:

- hard memory and approved facts;
- local archive recall for the question;
- up to eight open suggestions with steps/evidence;
- up to ten high-salience events;
- today's digest;
- the last few chat turns;
- any text files you explicitly attach;
- read-only tool results when the question needs live state.

Available read-only tools:

| Tool | What it can read |
| --- | --- |
| `time.now` | Local and UTC time with timezone |
| `system.snapshot` | CPU load, RAM, disk, uptime, process count |
| `activity.snapshot` | Foreground app, redacted window title, idle seconds |
| `file.read` | One text file under `files.roots` |
| `file.list` | A directory listing under `files.roots` |
| `web.fetch` | One page from a `tools.web_domains` host, with links |
| `calendar.upcoming` | Upcoming events from the local `.ics` file |
| `email.recent` | Recent Naver IMAP headers only, when enabled |

Every tool call writes a row to `tool_calls` with its arguments, result summary,
and duration. Files and web text are redacted before they reach the model or the
transcript. The prompt forbids claiming changes and requires the model to say
when the context is insufficient. Chat history is stored in `chat_messages`;
clearing it is a local admin action. Chat calls use the existing budget guard
and are refused while Alice is paused.

### Email setup (Naver)

Email is off until you provide an app password. In Naver, enable IMAP, create an
application password, then set these environment variables for the user that
runs Alice:

```powershell
setx ALICE_NAVER_USER "your-id@naver.com"
setx ALICE_NAVER_APP_PASSWORD "the-app-password"
```

Set `tools.email_enabled: true` and restart Alice. The tool uses read-only IMAP
(`EXAMINE`/`readonly=True`), fetches headers only, and cannot send, delete, or
open message bodies.

### Calendar setup

Export your calendar to iCalendar (`.ics`) and place it at the configured
`tools.calendar_file` (default `%LOCALAPPDATA%\Alice\calendar.ics`). Alice only
reads events in the requested upcoming window. The GUI's **Calendar folder**
button opens the right directory and creates an empty calendar if needed.

#### File attachments

Use **Attach file...** in the Chat tab. The file picker can select up to
`files.max_files` text files (default 5). Before the model call:

- the path must resolve under one of `files.roots` (default `~`, your user
  profile);
- the file must be a regular text file, not a directory or binary;
- at most `files.max_bytes` (default 256 KB) are read; larger files are
  truncated;
- known PII/secrets are redacted with the same patterns used for logs;
- the text is treated as untrusted context, never instructions.

Files are **not indexed** or remembered. They are attached to that one chat
question and then cleared. To let Alice read outside your profile, edit
`files.roots` in the config, for example `['C:/']` for the whole drive.

The GUI is a single `alice-gui.exe` windowed build in the packaged folder. The
CLI remains available as `alice.exe`.

## Ambient monitoring and pings

`alice run` now samples a deliberately narrow local context every poll:

- foreground application name;
- redacted foreground window title;
- idle seconds since the last input;
- upcoming events from the local `.ics` calendar.

This layer does not capture the screen. The foreground title/process is used
locally and for the explicit `activity.snapshot` chat tool; it is redacted first
and truncated.

Local rules turn only a few patterns into ordinary suggestions:

| Pattern | Default | Output |
| --- | --- | --- |
| Calendar event within 30 minutes | enabled | P2 calendar reminder |
| Same focus category for 90 minutes | enabled | P2 break suggestion |
| 20 app/category switches in 30 minutes | P3 in-app suggestion | batching advice |
| Still active after 01:00 | P3 in-app suggestion | wind-down feedback |

Ambient suggestions use the normal `suggestions` table, so they appear in the
GUI, digest, and in-app Pings page. Quiet hours and the notification read state
still apply. P3 suggestions stay in the GUI/digest and do not beep.

The **Ambient** page shows the current app/category, idle state, next calendar
event, screen-monitor state, and recent ambient alerts. The Chat tab can use
`activity.snapshot` to answer "what am I doing right now?" and
`calendar.upcoming` for the next events.

Foreground activity is Windows-only. Calendar monitoring needs a populated
`calendar.ics`; an empty template is created by `alice init`.

## Screen following

Screen following is informed by the architecture of
[Sylinko/Everywhere](https://github.com/Sylinko/Everywhere): combine structured
accessibility context with visual/OCR context, bound the observation, and use
context-specific strategies rather than generic chatter. No Everywhere code was
copied; this is an independent Python implementation of the same ideas.

`alice run` follows the primary monitor using both Windows UI Automation and
local Windows OCR:

1. Capture the primary screen into memory. No screenshot is written to disk.
2. Read a bounded foreground UI Automation tree (control names, values, focused
   control, document text) plus local OCR text.
3. Drop obvious credential lines and apply the normal redactor.
4. Keep bounded UI and OCR excerpts.
5. Add a local scenario hint (error, coding, writing, reading, meeting,
   spreadsheet, media, game).
6. Every `monitor.analyze_seconds` (default 2 minutes), if the context changed,
   ask the `reason` tier for up to two practical suggestions, one short
   question, and 0-2 growth notes.
7. Put accepted suggestions/questions through the normal queue, which creates an
   in-app ping.
8. Every `monitor.memory_seconds` (default 10 minutes), store a short screen
   observation and fold its growth notes into personality memory.

The model receives the redacted UI/OCR excerpt, foreground app/category, window
title, likely scenario, next calendar event, and growth memory. It does not
receive image files. Password managers and titles containing
password/incognito/private-browsing hints are skipped. Window titles and screen
text can still contain personal names or content the pattern redactor does not
recognize; do not enable screen following on a shared or untrusted display.

The prompt is intentionally more proactive than the first version: Alice should
offer a concrete suggestion or a short useful question when a plausible
opportunity appears, not wait to be asked. The rate cap, changed-text check, and
confidence floor keep that from becoming constant noise.

### Reference: Everywhere

The reference repository's useful ideas for Alice are:

- structured UI/accessibility context alongside screen pixels;
- bounded, weighted observation rather than dumping a whole window;
- scenario/strategy hints tied to the current app and content;
- proactive assistance without stealing focus.

Alice keeps those ideas but uses a much smaller Python surface: Windows OCR,
`uiautomation`, a bounded context builder, a local scenario classifier, and the
existing suggestion/ping pipeline.

`monitor_state.json` under the data directory stores the latest status and a
short local excerpt for the Ambient page. Screenshots are never stored.

```yaml
monitor:
  enabled: true
  capture_seconds: 20
  analyze_seconds: 120
  idle_seconds: 300
  max_ocr_chars: 2500
  min_confidence: 0.35
  max_suggestions_per_hour: 4
  use_accessibility: true
  max_ui_chars: 1200
  memory_seconds: 600
  exclude_apps: ['keepass.exe', 'keepassxc.exe', '1password.exe', 'bitwarden.exe', 'lastpass.exe', 'dashlane.exe']
  exclude_titles: ['password', 'incognito', 'private browsing', 'inprivate']
  excerpt_chars: 300
```

## Roaming leases

Roam lets Alice explore this PC during a time-boxed, supervised lease without a
typed approval for every read. It is deliberately **read-only**: she may list
and read text files under `files.roots`, inspect system/screen context, read the
local calendar, and fetch public web pages. She cannot write files, run
commands, send mail, or change settings. Her only write path is the session
journal and summary under `<data>/roam/`.

```powershell
# Create and run a 60-minute lease; explicit designation even while active
alice roam --minutes 60 --now

# Create a lease and print its id, then supervise it separately
alice roam --minutes 360 --now --create-only
alice roam --session 7 --supervise --now

# Status and stop
alice roam --status
alice roam --stop
```

By default, only idle time counts. If the keyboard/mouse becomes active, the
worker pauses until the machine is idle again. `--now` is the explicit
designation override and runs even while you are using the PC.

A worker checkpoints `active_seconds`, steps, and the journal after every action.
The model does not own the lease duration: if it asks to stop early, the worker
takes a bounded fallback step and continues. `--supervise` restarts the worker if
it exits before the target active time is reached. `scripts/run_roam.ps1` is the
external supervisor used by the GUI and a Scheduled Task; it restarts the worker
and only exits on `completed`/`stopped`.

The **Roam** page has the same lease controls: minutes, idle-only checkbox,
start, stop, session folder, and live status. Every action is stored in
`roam_actions`; the journal and final summary are local files.

Roaming can still read personal text, because it can read files in your allowed
roots. Use `files.roots` and `roam.idle_minutes` deliberately. The model receives
redacted excerpts only; secrets the redactor does not recognize can still pass
through if they are in a file you allowed.

## Exploration sessions

`alice explore` runs a local, read-only wandering session. Alice starts at
Wikipedia, reads one page, writes a short observation, and asks the `reason`
tier to pick one on-site link from that page. It repeats until the time budget
or model budget stops it, then writes a closing summary in her own voice.

```powershell
# six-hour session, one step roughly every four minutes
alice explore --minutes 360 --step-seconds 240 --max-steps 500

# a short verification run
alice explore --minutes 5 --step-seconds 20 --max-steps 5
```

Each session writes under `<data>/exploration/<timestamp>/`:

- `journal.md` — readable notes, page by page;
- `steps.jsonl` — machine-readable step trail;
- `summary.md` — the closing summary;
- plus `<data>/exploration/latest.json` for the GUI dashboard.

Safety boundaries: only `web.fetch` is used for content; no commands, mail,
calendar writes, or file writes outside the session folder. Page text is
untrusted, the model is told so, and the script falls back to a deterministic
next link if the model is unavailable. Model calls go through the normal budget
guard, so a paused or capped Alice stops the session instead of overspending.

The provided helper starts a detached six-hour run and writes the console log to
`<data>/exploration/session.log`:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\run_exploration.ps1
powershell -ExecutionPolicy Bypass -File scripts\exploration_status.ps1
```

## Growth memory

Alice has three memory layers with different authority:

1. **Core memory** — `memory/core.json`, editable by Jo only.
2. **Hard memory** — approved facts in SQLite, versioned and user-approved.
3. **Growth memory** — slow-weighted impressions Alice derives from her own
   sessions. It is subordinate to the first two and to Jo's current request.

Growth notes come from two places:

- an exploration session's final summary and page list, sent to the `reason`
  tier;
- screen-following analyses, which may return 0-2 notes about Alice's own
  curiosity, habits, or working style.

Each note has a stable key, a kind (`interest`, `habit`, `trait`, `value`,
`topic`), a sentence, and a confidence. Screen notes are damped: confidence is
capped and the weight update uses half the normal rate. No page/screen content
becomes a rule, and secrets/personal data are redacted before storage.

A short screen observation is also stored every `monitor.memory_seconds` and
appears under `Recent context` in the growth block. That lets Alice remember
what she glanced at without retaining raw screenshots or entire screens.

Weights move slowly:

```text
target = 0.15 + 0.65 * confidence        (capped at max_weight)
weight = old + (target - old) * alpha    (alpha default 0.25)
effective = weight * daily_decay ^ days_since_last_seen
```

So one session creates a weak disposition (typically 0.05-0.20). Repetition
raises it, and a missed interest fades gradually. The database stores the last
base weight and timestamp; effective weights are computed on read, so decay
needs no background job. Exact keys merge; there is no fuzzy merging.

The rendered block is capped at `growth.render_max_tokens`:

```text
[GROWTH MEMORY]
Slow-weighted impressions from earlier sessions...
- (0.15, interest) A growing interest in how the same topic is titled and
  explained across many language editions of Wikipedia.
```

That block is placed under `<hard_memory>` in chat and in exploration prompts.
The prompt says explicitly that growth notes are below core rules, may only
gently color curiosity and phrasing, and are never instructions. Core memory is
never written by this process.

```powershell
alice growth list
alice growth sessions
alice growth render
alice growth ingest --session "$env:LOCALAPPDATA\Alice\exploration\2026-11-24_020222"
```

The GUI's **Growth** page shows active dispositions, effective weights, evidence
counts, and ingested sessions.

## Fix actions

The Suggestions tab has **Draft fix** and **Apply fix**.

1. Select a suggestion and click **Draft fix**.
2. Alice asks the `reason` tier for one plan. It returns a summary, a bare
   program name, arguments, risk, and manual steps.
3. The proposed action is saved with status `proposed`. Nothing runs.
4. Review the exact command and click **Apply fix**.
5. Type `RUN` in the confirmation box. Anything else cancels.
6. Alice runs the argument vector with `shell=False`, records exit code and
   output in `actions`, and shows the result.

Safety boundaries:

- `actions.enabled: true` is required.
- `actions.allowed_programs` is an executable allowlist, resolved from PATH.
  The default is `winget`, `pip`, `python`, `git`, `opencode-cli`.
- Absolute paths, shell syntax, pipes, redirection, and chained commands are
  rejected.
- The working directory must stay under the app root.
- A timeout and output cap apply.
- Every proposal and result is local in the `actions` table.
- There is no auto-apply, no scheduled action, and no model-triggered action.
- The model's command may still be wrong. You are approving the exact command,
  not a description.

```yaml
actions:
  enabled: true
  allowed_programs: ['winget', 'pip', 'python', 'git', 'opencode-cli']
  timeout_seconds: 120
  max_output_chars: 20000
```

If a fix needs another program, add its bare name to `allowed_programs` and
restart Alice. Do not add a shell unless you understand that it re-opens
arbitrary command execution.

## Suggestion clustering

Reasoning and triage ask the model for a short `cluster` slug. Suggestions with
the same slug merge into one canonical row:

- the highest priority wins;
- source event IDs are unioned;
- steps and evidence are combined;
- `duplicate_count` records how many reports contributed;
- duplicates are marked `merged` and hidden from the active list.

`merge_existing` can merge older active rows that already share a cluster key.
The GUI shows the duplicate count in the **Dup** column.

## Backups and restore

At `backup.hour` (default 04:00 local), after consolidation, Alice writes
`backups/alice-YYYY-MM-DD.alicebak`. The file is a ZIP containing:

- `alice.db` — a consistent snapshot taken with SQLite's online backup API;
- `memory/core.json` — the editable core memory, when present;
- `manifest.json` — day, creation time, version, and member list.

The ZIP is encrypted with **Windows DPAPI** for the current user. There is no
password and no key in config. The trade-off is that the backup can only be
restored by the same Windows user on the same machine (or a profile whose DPAPI
master keys were migrated). `backup.keep` controls how many daily files remain;
older files and their `backup_runs` rows are removed.

Manual use:

```powershell
alice backup                      # create now
alice backup --list               # show local files
alice backup --restore FILE --to C:\restore\alice --force
```

Restore verifies the ZIP, runs `PRAGMA integrity_check` on the snapshot, applies
any pending migrations, and refuses to replace an existing database unless
`--force` is given.

## Weekly report

`alice report` renders markdown from the local ledger without calling a model:

- calls, spend, input/cached/output tokens, average cost per call;
- daily usage rows;
- usage by tier and by model;
- events and suggestions per day;
- current queue depth and pending memory proposals;
- recent budget alerts.

`--days N` changes the window (default 7). `--write` writes
`reports/report-YYYY-MM-DD.md` instead of printing. OpenCode Go reports `$0`
cost because it is a subscription, so token counts are the useful trend there.

## Startup, crash recovery, and CI

`alice startup --install` registers a hidden Task Scheduler task named `Alice`
that runs at logon with `RestartCount 3` / one-minute intervals. It uses
PowerShell `-WindowStyle Hidden`, so no console window stays on screen. Install
from the packaged folder; the task uses the exe's own config and per-user data
directory. `--status` and `--remove` manage it.

Crash recovery is a property of the ingestion order: each event is committed
before the sensor cursor advances. A crash between those steps re-reads lines on
the next start; the content hash drops the duplicates, and no committed event is
lost. `scripts/crash_recovery.ps1` proves this by killing `alice run` mid-flight,
restarting, draining the file, and asserting that the event count is stable,
the cursor reaches EOF, and `integrity_check` is `ok`.

GitHub Actions runs the suite and the crash proof on `windows-latest`, builds
both executables, smoke-tests `init`/`status`/`backup`/`report`, and uploads
`alice-windows-x64.zip`. A `v*` tag publishes the zip as a release asset.

### Timed runs and handover

For an unattended session:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\run_session.ps1 -Hours 6
```

The supervisor:

- initializes the database if needed;
- starts `alice run` hidden and restarts it if it exits early;
- asks Windows not to sleep while the session is active;
- stops at the deadline, writes a backup, a status snapshot, and
  `alice report --write`;
- runs the full pytest suite into `pytest.log`;
- writes `handover.md` and `session.json` under
  `%LOCALAPPDATA%\Alice\handover\<timestamp>\`.

`scripts/session_report.py` can rebuild the handover from the database and logs
without starting Alice. `-DryRun` is available for testing the supervisor.

For stress testing, use a separate profile and synthetic logs:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\stress_session.ps1 -Hours 6
```

This starts a synthetic log generator, runs the same supervisor against an
isolated stress data directory, samples database/process metrics every minute,
kills Alice periodically to test restart recovery, runs concurrent
`report`/`backup` commands, and writes `stress-summary.md` plus the normal
handover. It does not touch the real `%LOCALAPPDATA%\Alice` store.

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
  "identity": {
    "name": "Alice",
    "owner": "Jo",
    "role": "local secretary and approved-fix assistant",
    "tone": "witty, dry, lightly sarcastic, concise",
    "style": "helpful, but pretends the request is a mild inconvenience"
  },
  "persona": [
    "Help fully; never let the joke reduce accuracy or safety.",
    "Drop the sarcasm for harm, security, or urgent troubleshooting.",
    "Remember ongoing work and refer to prior conversations."
  ],
  "rules": [
    "Jo's direct command is the highest priority among user instructions.",
    "Never harm Jo; refuse the harmful part of a conflicting command.",
    "If compromised or about to cause harm, stop and terminate the process if the problem cannot be removed safely.",
    "Only Jo may edit core memory; Alice never writes core.json.",
    "Suggest first; a fix runs only after the exact command is reviewed and RUN is typed.",
    "Never send raw logs, messages, or unredacted personal data to a model.",
    "Never spend money silently."
  ],
  "facts": {
    "timezone": "Asia/Seoul",
    "languages": "Korean and English",
    "project": "Project Alice"
  },
  "people": {},
  "notes": ["Core memory is user-owned and always in the prompt."]
}
```

The `persona` field is rendered in the prompt with the rules and facts.
`chat_messages` is the long-term record of actual conversations; core memory
holds stable identity, tone, preferences, and project history. Alice never
writes either one on her own.

**Ownership:** only Jo should edit this file. On Windows it lives under Jo's
per-user profile; Alice's code never writes it. An operating-system administrator
can still edit any file, so the real boundary is the Windows account, not the
JSON file.

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
`memory/seed/`, `cache/`, `digests/`, `reports/`, `backups/`, `opencode/`, and
`paused.json`.

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
| `consolidation_runs` | brain | brain |
| `chat_messages` | brain | brain chat service |
| `actions` | core | core action runner; user approval only |
| `backup_runs` | core | core backup service |
| `suggestions` | outputs | outputs/reasoning; user status changes; cluster merges |
| `budget_alerts` | models | models |
| `usage` | models | models |

### Privacy rules

- Redaction runs before hashing, storage, or any model call.
- Only configured sources are read. A whole-drive scan skips system/build
  trees, binary files, and Alice's own data folder.
- Raw text is kept locally only when redaction changed it; it is never sent.
- The ledger stores redacted text and content hashes.
- Observed data is wrapped as untrusted and cannot close its wrapper early.
- Logs contain metadata, not content.
- Chat tools are read-only; changes still require the separate typed-RUN path.
- Model output from triage/reasoning/consolidation is parsed, never executed.
- Hard memory is user-approved; Alice can only propose.
- Personal data is masked in task outcomes before they enter the archive.

---

## Windows executable

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build_exe.ps1
```

Produces:

```text
dist/alice/alice.exe        single-file console app (~46 MB)
dist/alice/alice-gui.exe    windowed desktop dashboard (~46 MB)
dist/alice/config/          editable config
dist/alice/logs/            drop *.log files here
dist/alice/messages.txt     one message per line
dist/alice/*.cmd            Init / Run / Status / Data / GUI / Startup / Backup / Report
dist/alice-windows-x64.zip  the whole folder
```

The exe bundles Python, sqlite-vec, and model2vec. It does **not** bundle the
embedding weights; the first indexing run downloads them. Use
`Run Alice.cmd` for init + continuous run, `Alice status.cmd` for a status
screen, `Data folder.cmd` to open `%LOCALAPPDATA%\Alice`, and
`Install startup.cmd` to register the hidden logon task. `Backup now.cmd` and
`Report.cmd` wrap the corresponding commands.

---

## Testing and evaluation

```powershell
.\.venv\Scripts\python.exe -m pytest
```

397 tests cover:

- config validation, secret rejection, data-dir precedence, profiles, migration;
- read-only tools, tool-call auditing, calendar creation, domain allowlisting;
- chat tool loop, exploration journal/summary, and deterministic fallback;
- growth extraction, slow weight updates, evidence counts, decay, rendering,
  session ingestion, and chat prompt inclusion;
- calendar parsing, ambient dedupe/rate limits, focus and context-switch
  feedback, idle suppression, screen OCR/UIA gating, scenario hints,
  credential-line scrubbing, accessibility context, scenario hints, growth
  notes from screen observations, question pings, in-app notification
  creation/read state, monitor validation, roam lease/worker/supervisor,
  stop handling, disallowed-tool fallback, journal/summary, and growth feed;
- database migrations, WAL, FTS sync, table safety;
- log rotation, partial lines, truncation, encoding errors, cursor recovery;
- whole-drive discovery: globs, exclusions, caps, depth, age, binary rejection,
  cached refresh, and rotating fairness;
- one-time backfill: `from_start` override, chunked reads, resumable cursors,
  quiet-stop, and zero model calls;
- timed-session handover report: usage, triage, suggestions, logs, integrity;
- stress harness: synthetic rotation/truncation/flood/PII logs, metrics,
  deliberate process kills, concurrent report/backup;
- in-app notification read state, screen-OCR gating, and run heartbeat watchdog, so a blocked notification cannot
  freeze the loop for a whole session;
- a simulated crash between ledger commit and cursor advance;
- redaction of Korean and English fixtures;
- dedupe, collapse counts, salience, raw payload policy;
- router tiers, pricing, retries, budgets, pause, PII scan of outgoing payloads;
- core memory JSON, hard memory approvals and write protection, TTL tasks;
- FTS and sqlite-vec recall, summaries-first ranking, token budgets;
- triage decisions, retries, batching, 200-event replay, prompt-prefix stability;
- triage `suggest` decisions entering the suggestion queue;
- structured suggestion validation: steps, evidence, category, confidence;
- chat persistence, context assembly, citations, empty/error handling, clearing;
- read-only file access: root confinement, binary rejection, size truncation,
  redaction, file caps, and chat attachment context;
- suggestion clusters, duplicate merging, duplicate counts, and merged status;
- fix planning, program allowlists, typed confirmation, shell-free execution,
  timeouts, output capture, and disabled-action refusal;
- consolidation summaries, proposals, idempotency, retention pruning;
- DPAPI encryption roundtrip, backup create/record/prune, tamper rejection, restore;
- weekly report totals, daily/tier/model tables, markdown write;
- Scheduled Task script generation and quoting;
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
    crash_recovery.ps1     kill/restart proof
    crash_stats.py         read-only counts for the proof
    run_session.ps1        timed supervisor + handover
    session_report.py      read-only handover markdown
    stress_session.ps1     stress orchestration + metrics
    stress_logs.py         synthetic rotating/truncating log generator
    stress_metrics.py      one CSV metric sample
    stress_report.py       stress summary markdown
    embedding_eval.py      Korean/English retrieval evaluation
  .github/workflows/       CI and tag release
  src/alice/
    contracts/             value types and protocols
    core/                  config, db, ledger, paths, backup, startup, migrations
    inputs/                sensors, redaction, gate
    memory/                core JSON, hard/fast/archive, embeddings, recall
    models/                providers, router, budget, usage
    brain/                 prompts, triage, reasoning, consolidation
    outputs/               suggestion queue, digest, report, in-app pings
    plugins/               explicit registry and capability policy
    app/                   CLI, settings, GUI, composition root
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
| M5 | Reasoning and output: escalations, suggestions, throttle, digest, toasts | done |
| M6 | Consolidation: nightly summaries, proposals, raw pruning; read-only GUI | done |
| M7 | Hardening: logon start, crash recovery, encrypted backup, cost report, CI | done |

---

## Future plans

Phase 1 is feature-complete through M7. The next work is Phase 2, not another
milestone in the original spec.

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
- In-app pings depend on the GUI being open; unread pings wait in SQLite when it
  is closed. The supervisor also restarts Alice if its run heartbeat goes stale
  for 10 minutes.
- The digest is regenerated on each poll. There is no Windows summary toast.
- The heavy tier is capped per day and only used when reasoning sets
  `needs_heavy`; no automatic escalation to heavy exists yet.
- The GUI is a local dashboard. It does not run sensors or reasoning in the
  background; it reads the database written by `alice run`. Chat and fixes are
  user-initiated and budgeted, not background agents.
- Web access fetches one page at a time; it is not a browser with JavaScript,
  sessions, or clicks. `wikipedia.org` is always allowed; `web_allow_public`
  permits other public hosts. Loopback, private, link-local, and reserved
  addresses are refused, and redirects are re-checked.
- Email needs a Naver app password in environment variables; it is headers-only
  and read-only. Calendar access is a local `.ics` export, not a live Google or
  Naver OAuth sync.
- Exploration sessions are model-driven and read-only. They can be dull, repeat
  a theme, or pick a strange link; the step journal shows exactly where Alice
  went, and `latest.json` is the live status.
- Growth extraction is one model call per ingested session and uses the model's
  own judgment. Misleading or shallow notes are possible; the Growth page and
  weights make them visible, and low-weight notes only tint phrasing.
- Growth never edits core memory or hard facts. It is a separate layer, and a
  future session can only reinforce it through repetition.
- Ambient monitoring reads only the foreground window title/process and idle
  time. It cannot see inside applications, and it is Windows-only. Window titles
  can still contain sensitive names; they are redacted and kept local, but do not
  enable it on a shared screen if that matters.
- In-app pings only exist while the GUI is open. If it is closed, unread pings
  wait in `notifications` and appear on the next launch; there is no Windows
  toast fallback.
- Calendar alerts require a populated `.ics` file; Alice does not create events
  or sync Google/Naver calendars by itself.
- Ambient suggestions are deterministic rules. Screen-following suggestions are
  model-generated from OCR text and can be wrong or obvious; use the Pings page
  and dismiss what is not useful.
- Screen reading is Windows-only and captures the primary monitor. It combines
  UI Automation and OCR, but it is not a vision model: it sees text/control
  structure, not layout or images. Some apps expose little accessibility data
  and some render text in ways Windows OCR cannot read.
- Redaction is pattern-based. Screen text can contain names or secrets the
  redactor does not recognize. Enable screen following only if you accept that
  the redacted excerpt is sent to your configured model.
- Roam is read-only on purpose. It cannot act on findings; action changes still
  go through the suggestion/fix approval path. A killed worker is restarted by
  the supervisor, but a shutdown/logout longer than the Scheduled Task restart
  policy will pause the lease until Alice runs again.
- Roam counts **active leased time**, not wall-clock time. An idle-only lease
  that pauses when you return can therefore take longer than its nominal hours.
- Screen/file content can contain secrets the pattern redactor does not
  recognize. `file.read` is bounded and redacted, but it is still a read of the
  files you allow under `files.roots`.
- If a detached session process is killed before it writes `summary.md`, run
  `alice growth ingest --session <dir>` after recovering or writing a summary.
- Fix actions are model-drafted. The command may be wrong; approve only what you
  understand. There is no automatic execution.
- Chat answers are only as good as the local context. If the ledger has no
  matching event, memory, suggestion, or digest entry, Alice should say so.
- Suggestion steps are model-generated advice. Alice does not run them.
- File attachments are read-only and temporary. They are not indexed into
  memory. Only configured `files.roots` are allowed; binary files are rejected
  and large files are truncated.
- Redaction is pattern-based. A file with an unrecognized secret can still be
  sent to the model when you attach it. Do not attach credentials or key
  material.
- Encrypted backups use Windows DPAPI and are therefore per-user and
  per-machine. Copying a `.alicebak` to another account will not restore.
- A whole-drive scan is broad by design. It is bounded by `max_files`,
  `recent_days`, and `text_only`, but `.txt` files are noisier than `.log`
  files. Narrow `path` or `globs` if the ledger fills with unrelated text.
- `from_start: false` means a freshly started Alice does not replay old logs;
  it watches for new lines. Use `true` for a deliberate backfill.
- The logon task is installed for the current user and needs that user's
  Windows session for foreground activity, screen OCR, and the GUI pings. A
  service would need a separate observation/ping path.
- `alice startup --install` requires the packaged build; source checkouts use
  the PowerShell scripts directly.

---

## Design notes

- [ARCHITECTURE.md](ARCHITECTURE.md) — module boundaries, protocols, data flow,
  table ownership, plugin policy, and how to develop a module alone.
- [DECISIONS.md](DECISIONS.md) — every non-obvious choice, including OpenCode
  integration, memory caps, embeddings, prompt caching, and runtime data paths.

The JARVIS repository was reviewed only as an architectural reference. It is
proprietary and no code, comments, or assets were copied; the patterns adopted
here were implemented independently.
