-- M3: memory embeddings.
--
-- Vectors are stored as float32 blobs in a regular table. sqlite-vec supplies
-- vec_distance_cosine() when its extension is available; without it, recall
-- falls back to FTS5. This avoids fixing a vec0 dimension in the schema and
-- lets the embedding model change without a migration.

CREATE TABLE memory_embeddings (
  source TEXT NOT NULL,          -- event | summary
  source_id INTEGER NOT NULL,
  model TEXT NOT NULL,
  dim INTEGER NOT NULL,
  vector BLOB NOT NULL,
  created TEXT NOT NULL,
  PRIMARY KEY (source, source_id)
);
CREATE INDEX memory_embeddings_source ON memory_embeddings (source);
