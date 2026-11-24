-- M14: in-app suggestion pings. No Windows toasts.

CREATE TABLE notifications (
  id INTEGER PRIMARY KEY,
  created TEXT NOT NULL,
  kind TEXT NOT NULL,
  title TEXT NOT NULL,
  body TEXT NOT NULL,
  suggestion_id INTEGER,
  read INTEGER NOT NULL DEFAULT 0,
  read_at TEXT
);
CREATE INDEX notifications_unread ON notifications (read, id);
CREATE INDEX notifications_suggestion ON notifications (suggestion_id);

-- Existing queued suggestions should appear in the new in-app bell once.
INSERT INTO notifications (created, kind, title, body, suggestion_id, read)
SELECT created, COALESCE(category, 'suggestion'), 'Suggestion',
       text, id, 0
FROM suggestions
WHERE status = 'queued';
