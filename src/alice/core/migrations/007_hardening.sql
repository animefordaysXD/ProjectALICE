-- M7: backup bookkeeping.

CREATE TABLE backup_runs (
  day TEXT PRIMARY KEY,          -- local calendar day backed up
  created TEXT NOT NULL,
  path TEXT NOT NULL,
  size_bytes INTEGER NOT NULL
);
