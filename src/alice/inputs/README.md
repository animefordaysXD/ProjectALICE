# inputs

Read-only observation. Turns logs and messages into ledger events. Never calls
a model.

- `sensors/`: interface, rotation-safe log tailer, bounded whole-drive log
  scan with caching/exclusions, local message stub.
- `files.py`: explicit, root-confined read-only text attachments for chat;
  binary rejection, byte caps, and redaction.
- `redact.py`: RRN, phone, card, account, email, custom masks.
- `gate.py`: normalization, dedupe, salience, ingestion.

Public API: `build_sensors`, `poll_once`, `Sensor`, `Gate`, `Redactor`.

Owns no tables; writes events through `core.events`.
Owns config: `sources`, `gate`, `redaction`, `files`.

Rules:

- Never mutate a file or send anything anywhere.
- Redaction runs before hashing/storage.
- A sensor commits its cursor only after the ledger write.

Tests: `pytest tests/test_inputs_*.py`.
