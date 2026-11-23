# memory

What Alice remembers, in three layers plus the editable core file.

- `core_file.py`: `memory/core.json`, user-edited, always in the prompt, 2K cap.
  Renders identity, persona, rules, facts, people, and notes.
- `hard.py`: versioned, user-approved facts and pending proposals. Only
  `approve()` writes `hard_memory`; it records `approved_by='user'`.
- `fast.py`: per-task scratchpad with a TTL. Completion or expiry writes a
  `task_outcome` event and deletes the task row.
- `archive.py`: FTS5 + sqlite-vec recall over events and summaries. Summaries
  are ranked before raw events, and results respect a token budget.
- `embeddings.py`: lazy CPU embeddings via `model2vec`
  (`minishlab/potion-multilingual-128M`, 256 dims).
- `service.py`: the facade used by `app` and later by `brain`.
- `seed.py`: identity/rules/facts markdown templates.

Owns tables: `hard_memory`, `proposals`, `tasks`, `summaries`,
`memory_embeddings`.
Owns config: `memory` (`core_memory_file`, caps, TTL, recall, embedding).

Rules:

- Alice never writes `core.json`; it creates a template once and reads it.
- Alice code may write `proposals`, never `hard_memory`.
- Over the hard cap: fail loudly, never truncate silently.
- `recall()` prefers summaries over raw events and respects a token budget.

Tests: `pytest tests/test_memory_*.py tests/test_app_cli.py`.

