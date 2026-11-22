-- Alice schema, version 1.
-- Timestamps are ISO 8601 UTC text (e.g. 2026-11-21T09:30:00Z).

CREATE TABLE events (
  id INTEGER PRIMARY KEY,
  ts TEXT NOT NULL,
  source TEXT NOT NULL,
  kind TEXT NOT NULL,
  content_hash TEXT NOT NULL UNIQUE,
  text_redacted TEXT NOT NULL,
  raw_ref TEXT,
  salience REAL DEFAULT 0,
  status TEXT DEFAULT 'new'
);
CREATE INDEX events_status_ts ON events (status, ts);
CREATE INDEX events_source_ts ON events (source, ts);

CREATE TABLE cursors (
  source TEXT PRIMARY KEY,
  position TEXT NOT NULL,
  updated TEXT NOT NULL
);

CREATE TABLE hard_memory (
  key TEXT NOT NULL,
  version INTEGER NOT NULL,
  value TEXT NOT NULL,
  created TEXT NOT NULL,
  approved_by TEXT NOT NULL,
  active INTEGER DEFAULT 1,
  PRIMARY KEY (key, version)
);

CREATE TABLE proposals (
  id INTEGER PRIMARY KEY,
  key TEXT NOT NULL,
  proposed_value TEXT NOT NULL,
  reason TEXT,
  created TEXT NOT NULL,
  status TEXT DEFAULT 'pending'
);
CREATE INDEX proposals_status ON proposals (status, created);

CREATE TABLE tasks (
  id INTEGER PRIMARY KEY,
  goal TEXT NOT NULL,
  state TEXT,
  status TEXT DEFAULT 'open',
  created TEXT NOT NULL,
  expires TEXT NOT NULL
);
CREATE INDEX tasks_status_expires ON tasks (status, expires);

CREATE TABLE summaries (
  id INTEGER PRIMARY KEY,
  level TEXT NOT NULL,
  period_start TEXT NOT NULL,
  period_end TEXT NOT NULL,
  text TEXT NOT NULL
);
CREATE INDEX summaries_level_start ON summaries (level, period_start);

CREATE TABLE suggestions (
  id INTEGER PRIMARY KEY,
  created TEXT NOT NULL,
  priority INTEGER NOT NULL,
  text TEXT NOT NULL,
  source_event_ids TEXT,
  status TEXT DEFAULT 'queued'
);
CREATE INDEX suggestions_status_created ON suggestions (status, created);

CREATE TABLE usage (
  id INTEGER PRIMARY KEY,
  ts TEXT NOT NULL,
  tier TEXT NOT NULL,
  model TEXT NOT NULL,
  input_tokens INTEGER,
  cached_tokens INTEGER,
  output_tokens INTEGER,
  cost_usd REAL,
  purpose TEXT
);
CREATE INDEX usage_ts ON usage (ts);

-- Full-text search over redacted event text and summaries.
-- External-content FTS5 keeps the index in sync through triggers.

CREATE VIRTUAL TABLE events_fts USING fts5(
  text_redacted,
  content = 'events',
  content_rowid = 'id',
  tokenize = 'unicode61'
);

CREATE TRIGGER events_fts_ai AFTER INSERT ON events BEGIN
  INSERT INTO events_fts (rowid, text_redacted) VALUES (new.id, new.text_redacted);
END;
CREATE TRIGGER events_fts_ad AFTER DELETE ON events BEGIN
  INSERT INTO events_fts (events_fts, rowid, text_redacted) VALUES ('delete', old.id, old.text_redacted);
END;
CREATE TRIGGER events_fts_au AFTER UPDATE ON events BEGIN
  INSERT INTO events_fts (events_fts, rowid, text_redacted) VALUES ('delete', old.id, old.text_redacted);
  INSERT INTO events_fts (rowid, text_redacted) VALUES (new.id, new.text_redacted);
END;

CREATE VIRTUAL TABLE summaries_fts USING fts5(
  text,
  content = 'summaries',
  content_rowid = 'id',
  tokenize = 'unicode61'
);

CREATE TRIGGER summaries_fts_ai AFTER INSERT ON summaries BEGIN
  INSERT INTO summaries_fts (rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER summaries_fts_ad AFTER DELETE ON summaries BEGIN
  INSERT INTO summaries_fts (summaries_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;
CREATE TRIGGER summaries_fts_au AFTER UPDATE ON summaries BEGIN
  INSERT INTO summaries_fts (summaries_fts, rowid, text) VALUES ('delete', old.id, old.text);
  INSERT INTO summaries_fts (rowid, text) VALUES (new.id, new.text);
END;
