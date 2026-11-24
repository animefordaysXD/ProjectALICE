## Outputs and delivery

Implemented:

- `queue.py`: suggestion queue, quiet hours, priority threshold, structured
  steps/evidence/category/confidence, user status changes (`done`, `dismissed`),
  and in-app notification creation for every new suggestion.
- `inbox.py`: notification read state for the GUI bell, banner, and Pings page.
- `digest.py`: daily markdown digest at `<data>/digests/YYYY-MM-DD.md`, budget
  alerts, pending proposals, suggestion steps and evidence.
- `report.py`: weekly markdown at `<data>/reports/report-YYYY-MM-DD.md` with
  spend, token, tier, model, and activity trends. No model calls.
- `notify.py`: legacy Windows toast adapter, disabled by default and unused by
  the runtime.

Owns tables: `suggestions` (including `shown_at`, `reason`, `steps`, `evidence`,
`category`, `confidence`) and `notifications`.
Owns config: `notify`.

Reads budget alerts through an injected `BudgetSnapshot`; it does not import
`models`.

Rules:

- Never decide that something is important; only deliver.
- Quiet hours and throttles are enforced here.
- Pings are in-app only; unread notifications wait in SQLite when the GUI closes.
- The digest always works even when the GUI is closed.

Tests: `pytest tests/test_outputs_*.py`.
