-- M8: richer suggestions and the local chat transcript.

ALTER TABLE suggestions ADD COLUMN steps TEXT;
ALTER TABLE suggestions ADD COLUMN evidence TEXT;
ALTER TABLE suggestions ADD COLUMN category TEXT;
ALTER TABLE suggestions ADD COLUMN confidence REAL;
ALTER TABLE suggestions ADD COLUMN status_changed TEXT;

CREATE TABLE chat_messages (
  id INTEGER PRIMARY KEY,
  created TEXT NOT NULL,
  role TEXT NOT NULL,              -- user | assistant
  content TEXT NOT NULL,
  citations TEXT,                  -- JSON list of {kind,id,label}
  input_tokens INTEGER NOT NULL DEFAULT 0,
  output_tokens INTEGER NOT NULL DEFAULT 0,
  cost_usd REAL NOT NULL DEFAULT 0,
  error TEXT
);
CREATE INDEX chat_messages_created ON chat_messages (created);
