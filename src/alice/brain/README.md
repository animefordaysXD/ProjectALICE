# brain

Policy and reasoning. The only module that decides what matters.

Planned:

- `consolidate.py` (M6): nightly summaries and proposals.

Implemented:

- `reason.py`: Tier 2 escalation reasoning, task lifecycle, recall, optional
  heavy refinement, validated suggestions.
- `prompts.py`: static prompts for triage and reasoning, hard memory, task and
  recall blocks, observed events, cache-prefix length.
- `triage.py`: Tier 1 batching, decisions, ledger statuses.

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
