# app

Composition root and process lifecycle. The only module allowed to import every
other module.

- `cli.py`: `init`, `run`, `status`, `pause`, `resume`, `approve`,
  `consolidate`, `backup`, `report`, `startup`, `gui`.
- `settings.py`: composes per-module config into `AppSettings`.
- `bootstrap.py`: opens the database and wires sensors → gate → ledger, models,
  memory, reasoning, consolidation, backup, chat, and delivery.
- `dashboard.py`: read-only dashboard data.
- `gui.py`: Tkinter dashboard with suggestion actions and chat. Read-only plus
  explicit admin actions.
- Future: a service host if toasts and per-user DPAPI stop being requirements.

Owns no tables and contains no domain policy.

Rules:

- Cross-module protocols are wired here; modules never import each other.
- Swapping an implementation (fake model, alternate notifier) happens here.
- A CLI command should be a thin wrapper over module services.

Tests: `pytest tests/test_app_*.py`.
