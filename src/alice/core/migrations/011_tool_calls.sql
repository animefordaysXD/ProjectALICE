-- M11: tool-call audit for read-only tools and exploration.

CREATE TABLE tool_calls (
  id INTEGER PRIMARY KEY,
  created TEXT NOT NULL,
  tool TEXT NOT NULL,
  args TEXT,
  ok INTEGER NOT NULL DEFAULT 1,
  summary TEXT,
  duration_ms INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX tool_calls_created ON tool_calls (created);
CREATE INDEX tool_calls_tool ON tool_calls (tool, created);
