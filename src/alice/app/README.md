# app

Composition root and process lifecycle. The only module allowed to import every
other module.

- `cli.py`: `init`, `run`, `status`, `pause`, `resume`, `approve`.
- `settings.py`: composes per-module config into `AppSettings`.
- `bootstrap.py`: opens the database and wires sensors → gate → ledger, plus
  models and memory.
- Future: scheduler, Windows Task Scheduler/service entry.

Owns no tables and contains no domain policy.

Rules:

- Cross-module protocols are wired here; modules never import each other.
- Swapping an implementation (fake model, alternate notifier) happens here.
- A CLI command should be a thin wrapper over module services.

Tests: `pytest tests/test_app_*.py`.
