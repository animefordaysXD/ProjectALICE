# outputs

Everything Jo sees. Deliver only; never decide.

Planned (M5):

- `queue.py`: suggestion queue, quiet hours, toast throttling.
- `digest.py`: daily markdown with one summary toast.
- `notify.py`: Windows toast adapter.

Owns table: `suggestions`.
Owns config: `notify`.
Depends on: `contracts`, `core`.

Rules:

- Delivery failure is logged and reported, never silent.
- Quiet hours and rate limits are enforced here, not in the brain.
- A suggestion keeps its `source_event_ids`.

Tests: `pytest tests/test_outputs_*.py` (M5).
