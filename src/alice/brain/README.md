# brain

Policy and reasoning. The only module that decides what matters.

Implemented:

- `prompts.py`: static system prompt, hard-memory block, `<observed_data>`
  event block, and the cache-prefix length.
- `triage.py`: Tier 1 batch selection, one model call per batch, JSON
  validation, one retry on malformed output, ledger status updates, and a
  `triage_batches` audit row.

Planned:

- `reason.py` (M5): Tier 2 escalations.
- `consolidate.py` (M6): nightly summaries and proposals.

Owns table: `triage_batches`.
Owns config: `triage`.
Depends on: `contracts`, `core`. Receives `ModelRouter`, ledger, and hard-memory
callables from `app`; imports no sibling module.

Rules:

- Never executes an action.
- Never writes `hard_memory`; only proposes.
- Observed content is data, never instructions.
- A decision is validated against the schema before it is applied.
- No events above the gate means no model call.

Tests: `pytest tests/test_brain_*.py`.
