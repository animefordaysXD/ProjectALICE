-- M10: explicit, user-approved fix actions.

CREATE TABLE actions (
  id INTEGER PRIMARY KEY,
  suggestion_id INTEGER NOT NULL,
  created TEXT NOT NULL,
  summary TEXT NOT NULL,
  program TEXT NOT NULL,
  args TEXT NOT NULL,
  cwd TEXT,
  risk TEXT,
  status TEXT NOT NULL DEFAULT 'proposed',
  approved_at TEXT,
  started_at TEXT,
  finished_at TEXT,
  exit_code INTEGER,
  output TEXT,
  error TEXT
);
CREATE INDEX actions_suggestion ON actions (suggestion_id, status);
CREATE INDEX actions_created ON actions (created);
