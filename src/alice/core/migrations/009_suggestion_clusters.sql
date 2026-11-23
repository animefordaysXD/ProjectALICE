-- M9: suggestion clustering and merge bookkeeping.

ALTER TABLE suggestions ADD COLUMN cluster_key TEXT;
ALTER TABLE suggestions ADD COLUMN duplicate_count INTEGER NOT NULL DEFAULT 1;
ALTER TABLE suggestions ADD COLUMN merged_into INTEGER;

CREATE INDEX suggestions_cluster ON suggestions (cluster_key, status);
