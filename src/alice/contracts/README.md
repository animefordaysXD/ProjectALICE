# contracts

Pure interfaces and shared value types. Imports nothing from Alice and has no
I/O.

- `models.py`: `Message`, `Completion`, `ModelRouter`.
- `memory.py`: `MemoryItem`, `Recall`, `MemoryStore`.
- `outputs.py`: `Suggestion`, `SuggestionSink`, `Notifier`.
- `plugins.py`: `PluginManifest`, `Plugin`, `PluginContext`.

Rules:

- No database, config, logging, network, or filesystem code here.
- If two modules need a type, it lives here.
- Models in this package are frozen dataclasses that validate their own inputs.

Tests: `pytest tests/test_plugins.py tests/test_architecture.py`.
