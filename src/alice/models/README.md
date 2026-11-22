# models

How Alice talks to models. No module knows a model name except through config.

- `base.py`: `Provider` interface and errors.
- `router.py`: tier → model, pricing, one retry, usage logging.
- `anthropic.py`: direct Messages API adapter (lazy SDK import).
- `opencode.py`: OpenCode CLI adapter (OpenCode Go).
- `fake.py`: deterministic offline provider.
- `budget.py`: `usage` log, caps, kill switch.

Owns table: `usage`.
Owns config: `models`, `budget`.

Rules:

- Every call passes the budget guard.
- Paid tiers require pricing; subscription providers report their own cost.
- Providers never know about triage, memory, or suggestions.
- Keys are never stored in config, logs, or the database.

Tests: `pytest tests/test_models_router*.py`.
