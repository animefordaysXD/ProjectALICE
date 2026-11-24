# app

Composition root and process lifecycle. The only module allowed to import every
other module.

- `cli.py`: `init`, `run`, `status`, `pause`, `resume`, `approve`, `explore`,
  `growth`, `monitor`, `consolidate`, `backup`, `report`, `startup`, `gui`,
  `tools`.
- `settings.py`: composes per-module config into `AppSettings`.
- `bootstrap.py`: opens the database and wires sensors → gate → ledger, models,
  memory, reasoning, consolidation, backup, chat, ambient, screen monitor, and
  delivery.
- `dashboard.py`: read-only dashboard data, current activity, calendar, growth,
  pings, and ambient state.
- `gui.py`: Tkinter dashboard with in-app pings, suggestion actions, chat,
  read-only file attachments, fix drafting, and typed `RUN` confirmation.
- `activity.py`, `ambient.py`, `calendar.py`, `screen.py`, `monitor.py`:
  passive local context and screen-following suggestions.
- Future: a service host if per-user DPAPI and desktop observation stop being
  requirements.

Owns tables: `tool_calls`, `ambient_alerts` (plus monitor state JSON).

Rules:

- Cross-module protocols are wired here; modules never import each other.
- Swapping an implementation (fake model, alternate notifier) happens here.
- A CLI command should be a thin wrapper over module services.

Tests: `pytest tests/test_app_*.py`.
