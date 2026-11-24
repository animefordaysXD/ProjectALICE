-- M12: slow-weighted growth memory learned from exploration sessions.

CREATE TABLE growth_memories (
  id INTEGER PRIMARY KEY,
  key TEXT NOT NULL UNIQUE,
  kind TEXT NOT NULL,                -- interest | habit | trait | value | topic
  text TEXT NOT NULL,
  weight REAL NOT NULL DEFAULT 0,     -- 0..1, decays slowly
  confidence REAL NOT NULL DEFAULT 0, -- last extraction confidence
  evidence_count INTEGER NOT NULL DEFAULT 1,
  source TEXT,
  created TEXT NOT NULL,
  last_seen TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX growth_memories_weight ON growth_memories (active, weight DESC);

CREATE TABLE growth_sessions (
  id INTEGER PRIMARY KEY,
  created TEXT NOT NULL,
  session_dir TEXT,
  started TEXT,
  ended TEXT,
  steps INTEGER NOT NULL DEFAULT 0,
  status TEXT,
  summary TEXT NOT NULL,
  themes TEXT,
  input_tokens INTEGER NOT NULL DEFAULT 0,
  output_tokens INTEGER NOT NULL DEFAULT 0,
  cost_usd REAL NOT NULL DEFAULT 0
);
CREATE INDEX growth_sessions_created ON growth_sessions (created);
