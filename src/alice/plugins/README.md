# plugins

Optional extensions with declared capabilities. Built-ins are registered by
`app`; nothing is discovered from disk by default.

- `registry.py`: register, look up, filter by capability, start/stop with
  failure isolation.

Capabilities: `sensor`, `notifier`, `job`. There is no action capability in
Phase 1.

Rules:

- Plugins are trusted in-process Python; there is no sandbox.
- A plugin receives a narrow context, not the database, config, or secrets.
- A failing plugin is logged and skipped; it cannot stop Alice.
- Third-party loading, if added, must be opt-in.

Tests: `pytest tests/test_plugins.py`.
