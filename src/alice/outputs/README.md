## Outputs and delivery

Implemented:

- `queue.py`: suggestion queue, quiet hours, hourly toast cap, priority
  threshold. Failed toasts stay queued.
- `digest.py`: daily markdown digest at `<data>/digests/YYYY-MM-DD.md`, one
  summary toast on first write, budget alerts, pending proposals.
- `notify.py`: lazy `win11toast` delivery with a no-op fallback.

Owns table: `suggestions` (including `shown_at` and `reason`).
Owns config: `notify`.

Reads budget alerts through an injected `BudgetSnapshot`; it does not import
`models`.

Rules:

- Never decide that something is important; only deliver.
- Quiet hours and throttles are enforced here.
- Delivery failure is logged and the suggestion remains queued.
- The digest always works even when toasts do not.

Tests: `pytest tests/test_outputs_*.py`.
