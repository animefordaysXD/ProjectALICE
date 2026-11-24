-- M15: time-boxed, supervised, read-only PC roaming sessions.

CREATE TABLE roam_sessions (
  id INTEGER PRIMARY KEY,
  created TEXT NOT NULL,
  started TEXT,
  ended TEXT,
  target_seconds INTEGER NOT NULL,
  active_seconds INTEGER NOT NULL DEFAULT 0,
  steps INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'created',
  stop_reason TEXT,
  session_dir TEXT,
  summary_path TEXT,
  last_step TEXT
);
CREATE INDEX roam_sessions_status ON roam_sessions (status, id);

CREATE TABLE roam_actions (
  id INTEGER PRIMARY KEY,
  session_id INTEGER NOT NULL,
  created TEXT NOT NULL,
  action TEXT NOT NULL,
  args TEXT,
  ok INTEGER NOT NULL DEFAULT 1,
  summary TEXT,
  duration_ms INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX roam_actions_session ON roam_actions (session_id, id);
