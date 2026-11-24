-- M13: passive ambient alerts (calendar, focus, context switching).

CREATE TABLE ambient_alerts (
  id INTEGER PRIMARY KEY,
  created TEXT NOT NULL,
  kind TEXT NOT NULL,
  alert_key TEXT NOT NULL,
  title TEXT NOT NULL,
  body TEXT NOT NULL,
  suggestion_id INTEGER
);
CREATE UNIQUE INDEX ambient_alerts_unique ON ambient_alerts (kind, alert_key);
CREATE INDEX ambient_alerts_created ON ambient_alerts (created);
