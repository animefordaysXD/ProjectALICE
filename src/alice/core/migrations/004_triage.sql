-- M4: one row per triage batch.
--
-- Triage is batched: one model decision covers the events in that batch. The
-- ledger records per-event status; this table keeps the decision, reason, and
-- optional suggestion/recall text for M5.

CREATE TABLE triage_batches (
  id INTEGER PRIMARY KEY,
  created TEXT NOT NULL,
  event_ids TEXT NOT NULL,        -- JSON array of event ids
  decision TEXT NOT NULL,         -- ignore | log | suggest | escalate
  priority INTEGER NOT NULL,
  reason TEXT NOT NULL,
  suggestion_text TEXT,
  recall_query TEXT,
  attempts INTEGER NOT NULL DEFAULT 1,
  input_tokens INTEGER,
  output_tokens INTEGER,
  cost_usd REAL
);
CREATE INDEX triage_batches_created ON triage_batches (created);
