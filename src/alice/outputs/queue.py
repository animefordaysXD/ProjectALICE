"""Suggestion queue with quiet hours and toast throttling."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from ..contracts.outputs import Notifier, Suggestion, SuggestionSink
from .config import Notify


@dataclass(frozen=True)
class QueuedSuggestion:
    id: int
    created: str
    priority: int
    text: str
    reason: str | None
    source_event_ids: tuple[int, ...]
    status: str
    shown_at: str | None


class SuggestionQueue(SuggestionSink):
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def submit(
        self, suggestion: Suggestion, *, created: str | None = None
    ) -> int:
        from ..core.db import utc_now

        cursor = self.conn.execute(
            """
            INSERT INTO suggestions
              (created, priority, text, reason, source_event_ids, status)
            VALUES (?, ?, ?, ?, ?, 'queued')
            """,
            (
                created or utc_now(),
                suggestion.priority,
                suggestion.text,
                suggestion.reason,
                json.dumps(list(suggestion.source_event_ids)),
            ),
        )
        self.conn.commit()
        return int(cursor.lastrowid)

    def pending(self, limit: int = 50) -> list[QueuedSuggestion]:
        rows = self.conn.execute(
            """
            SELECT * FROM suggestions
            WHERE status = 'queued'
            ORDER BY priority, created
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [_row(row) for row in rows]

    def deliver(
        self,
        notifier: Notifier,
        config: Notify,
        *,
        now: datetime | None = None,
    ) -> int:
        moment = now or datetime.now(timezone.utc)
        local = moment.astimezone()
        stamp = moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        if "toast" not in config.mode:
            return 0
        if is_quiet(local, config.quiet_hours):
            return 0

        hour_ago = (
            moment.astimezone(timezone.utc) - timedelta(hours=1)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        shown = self.conn.execute(
            "SELECT COUNT(*) FROM suggestions WHERE shown_at IS NOT NULL AND shown_at >= ?",
            (hour_ago,),
        ).fetchone()[0]
        remaining = config.max_toasts_per_hour - int(shown)
        if remaining <= 0:
            return 0

        rows = self.conn.execute(
            """
            SELECT * FROM suggestions
            WHERE status = 'queued' AND priority <= ?
            ORDER BY priority, created
            LIMIT ?
            """,
            (config.min_toast_priority, remaining),
        ).fetchall()

        delivered = 0
        for row in rows:
            if notifier.notify("Alice", row["text"]):
                self.conn.execute(
                    "UPDATE suggestions SET status = 'shown', shown_at = ? WHERE id = ?",
                    (stamp, row["id"]),
                )
                self.conn.commit()
                delivered += 1
        return delivered


def is_quiet(moment: datetime, quiet_hours: str) -> bool:
    """True when the local time falls inside an overnight or same-day range."""
    start_text, end_text = quiet_hours.split("-", 1)
    start = _minutes(start_text)
    end = _minutes(end_text)
    current = moment.hour * 60 + moment.minute
    if start <= end:
        return start <= current < end
    return current >= start or current < end


def _minutes(value: str) -> int:
    hour, minute = value.split(":", 1)
    return int(hour) * 60 + int(minute)


def _row(row: sqlite3.Row) -> QueuedSuggestion:
    try:
        ids = tuple(int(value) for value in json.loads(row["source_event_ids"] or "[]"))
    except (TypeError, ValueError, json.JSONDecodeError):
        ids = ()
    return QueuedSuggestion(
        id=int(row["id"]),
        created=str(row["created"]),
        priority=int(row["priority"]),
        text=str(row["text"]),
        reason=row["reason"],
        source_event_ids=ids,
        status=str(row["status"]),
        shown_at=row["shown_at"],
    )
