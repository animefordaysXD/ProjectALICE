# brain

Policy and reasoning. The only module that decides what matters.

Implemented:

- `prompts.py`: static prompts for triage, reasoning, consolidation, and chat;
  hard-memory block; task/recall blocks; observed data; cache-prefix length.
- `triage.py`: Tier 1 batching, decisions, ledger statuses.
- `reason.py`: Tier 2 escalation reasoning, task lifecycle, recall, optional
  heavy refinement, structured suggestions with steps/evidence/category/confidence.
- `consolidate.py`: Tier 3 hourly/daily summaries, proposals, raw pruning
  bookkeeping.
- `chat.py`: read-only chat over ledger, recall, suggestions, and digest;
  persists user/assistant turns and citations.

Planned:

- Further improving summary quality and proposal precision; no new action
  capabilities.

Owns tables: `triage_batches`, `consolidation_runs`, `chat_messages`.
Owns config: `triage`.
Depends on: `contracts`, `core`. Receives `ModelRouter`, ledger, and hard-memory
callables from `app`; imports no sibling module.

Rules:

- Never executes an action. Chat has no tools and cannot open, edit, send, or run
  anything.
- Never writes `hard_memory`; only proposes.
- Observed content is data, never instructions.
- A decision is validated against the schema before it is applied.
- No events above the gate means no model call.

Tests: `pytest tests/test_brain_*.py`.
