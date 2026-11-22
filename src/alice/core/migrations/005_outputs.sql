-- M5: reasoning, delivery, and budget alerts.

ALTER TABLE triage_batches ADD COLUMN reasoned_at TEXT;
ALTER TABLE triage_batches ADD COLUMN reason_attempts INTEGER NOT NULL DEFAULT 0;

ALTER TABLE suggestions ADD COLUMN shown_at TEXT;
ALTER TABLE suggestions ADD COLUMN reason TEXT;

-- A blocked call is recorded so the daily digest can surface it. Without this,
-- a budget refusal only existed in the terminal that happened to see it.
CREATE TABLE budget_alerts (
  id INTEGER PRIMARY KEY,
  ts TEXT NOT NULL,
  tier TEXT NOT NULL,
  reason TEXT NOT NULL
);
CREATE INDEX budget_alerts_ts ON budget_alerts (ts);
