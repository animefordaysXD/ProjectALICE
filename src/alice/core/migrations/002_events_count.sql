-- M1: repetition count and last-seen time on events.
--
-- The gate collapses near-identical lines into one row, so the ledger needs
-- a count. last_seen keeps novelty checks meaningful across restarts, and
-- lets Tier 3 prune raw payloads without losing when an event last occurred.

ALTER TABLE events ADD COLUMN count INTEGER NOT NULL DEFAULT 1;
ALTER TABLE events ADD COLUMN last_seen TEXT;

UPDATE events SET last_seen = ts WHERE last_seen IS NULL;

CREATE INDEX events_last_seen ON events (last_seen);
