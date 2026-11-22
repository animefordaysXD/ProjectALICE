-- M6: nightly consolidation bookkeeping.

CREATE TABLE consolidation_runs (
  day TEXT PRIMARY KEY,          -- local calendar day summarized
  created TEXT NOT NULL,
  summary_count INTEGER NOT NULL DEFAULT 0,
  proposal_count INTEGER NOT NULL DEFAULT 0,
  raw_deleted INTEGER NOT NULL DEFAULT 0
);
