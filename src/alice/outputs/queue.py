"""Suggestion queue with quiet hours and toast throttling."""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from ..contracts.outputs import Notifier, Suggestion, SuggestionSink
from ..core.db import utc_now
from .config import Notify
from .inbox import NotificationCenter

logger = logging.getLogger(__name__)


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
    steps: tuple[str, ...] = ()
    evidence: str | None = None
    category: str | None = None
    confidence: float | None = None
    cluster: str | None = None
    duplicate_count: int = 1


class SuggestionQueue(SuggestionSink):
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def submit(
        self, suggestion: Suggestion, *, created: str | None = None
    ) -> int:
        cluster = normalize_cluster(suggestion.cluster)
        if cluster:
            existing = self.conn.execute(
                "SELECT * FROM suggestions "
                "WHERE status IN ('queued', 'shown') AND cluster_key = ? "
                "ORDER BY id LIMIT 1",
                (cluster,),
            ).fetchone()
            if existing is not None:
                return self._merge_into(existing, suggestion)

        cursor = self.conn.execute(
            """
            INSERT INTO suggestions
              (created, priority, text, reason, source_event_ids, status,
               steps, evidence, category, confidence, cluster_key)
            VALUES (?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?)
            """,
            (
                created or utc_now(),
                suggestion.priority,
                suggestion.text,
                suggestion.reason,
                json.dumps(list(suggestion.source_event_ids)),
                json.dumps(list(suggestion.steps)),
                suggestion.evidence,
                suggestion.category,
                suggestion.confidence,
                cluster,
            ),
        )
        suggestion_id = int(cursor.lastrowid)
        try:
            NotificationCenter(self.conn).add(
                kind=suggestion.category or "suggestion",
                title=_notification_title(suggestion.category),
                body=suggestion.text,
                suggestion_id=suggestion_id,
            )
        except Exception:
            logger.exception("could not add in-app notification")
        self.conn.commit()
        return suggestion_id

    def merge_existing(self) -> int:
        """Merge active suggestions that share a cluster key."""
        rows = self.conn.execute(
            "SELECT * FROM suggestions "
            "WHERE status IN ('queued', 'shown') AND cluster_key IS NOT NULL "
            "ORDER BY priority, id"
        ).fetchall()
        groups: dict[str, sqlite3.Row] = {}
        merged = 0
        for row in rows:
            key = str(row["cluster_key"])
            canonical = groups.get(key)
            if canonical is None:
                groups[key] = row
                continue
            self._merge_rows(canonical, row)
            groups[key] = self.conn.execute(
                "SELECT * FROM suggestions WHERE id = ?", (canonical["id"],)
            ).fetchone()
            merged += 1
        return merged

    def _merge_into(
        self, canonical: sqlite3.Row, incoming: Suggestion
    ) -> int:
        source_ids = _ids(canonical["source_event_ids"]) | set(
            incoming.source_event_ids
        )
        steps = list(_steps(canonical["steps"]))
        for step in incoming.steps:
            if step not in steps:
                steps.append(step)
        self.conn.execute(
            """
            UPDATE suggestions SET
              priority = MIN(priority, ?),
              duplicate_count = duplicate_count + 1,
              source_event_ids = ?,
              steps = ?,
              evidence = COALESCE(evidence, ?),
              category = COALESCE(category, ?),
              confidence = COALESCE(confidence, ?),
              cluster_key = COALESCE(cluster_key, ?)
            WHERE id = ?
            """,
            (
                incoming.priority,
                json.dumps(sorted(source_ids)),
                json.dumps(steps),
                incoming.evidence,
                incoming.category,
                incoming.confidence,
                normalize_cluster(incoming.cluster),
                canonical["id"],
            ),
        )
        self.conn.commit()
        return int(canonical["id"])

    def _merge_rows(self, canonical: sqlite3.Row, duplicate: sqlite3.Row) -> None:
        source_ids = _ids(canonical["source_event_ids"]) | _ids(
            duplicate["source_event_ids"]
        )
        steps = list(_steps(canonical["steps"]))
        for step in _steps(duplicate["steps"]):
            if step not in steps:
                steps.append(step)
        self.conn.execute(
            """
            UPDATE suggestions SET
              priority = MIN(priority, ?),
              duplicate_count = duplicate_count + ?,
              source_event_ids = ?,
              steps = ?,
              evidence = COALESCE(evidence, ?),
              category = COALESCE(category, ?),
              confidence = COALESCE(confidence, ?)
            WHERE id = ?
            """,
            (
                int(duplicate["priority"]),
                int(duplicate["duplicate_count"] or 1),
                json.dumps(sorted(source_ids)),
                json.dumps(steps),
                duplicate["evidence"],
                duplicate["category"],
                duplicate["confidence"],
                canonical["id"],
            ),
        )
        self.conn.execute(
            "UPDATE suggestions SET status = 'merged', merged_into = ?, "
            "status_changed = ? WHERE id = ?",
            (canonical["id"], utc_now(), duplicate["id"]),
        )
        self.conn.commit()

    def set_status(self, suggestion_id: int, status: str) -> None:
        if status not in ("queued", "shown", "done", "dismissed"):
            raise ValueError(f"unsupported suggestion status: {status}")
        self.conn.execute(
            "UPDATE suggestions SET status = ?, status_changed = ? WHERE id = ?",
            (status, utc_now(), suggestion_id),
        )
        self.conn.commit()

    def get(self, suggestion_id: int) -> QueuedSuggestion | None:
        row = self.conn.execute(
            "SELECT * FROM suggestions WHERE id = ?", (suggestion_id,)
        ).fetchone()
        return None if row is None else _row(row)

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


def _notification_title(category: str | None) -> str:
    return {
        "calendar": "Calendar",
        "wellbeing": "Ambient",
        "monitor": "Screen",
        "performance": "Performance",
        "security": "Security",
        "maintenance": "Maintenance",
        "workflow": "Workflow",
        "data": "Data",
    }.get((category or "").casefold(), "Suggestion")


def normalize_cluster(value: str | None) -> str | None:
    if value is None:
        return None
    slug = re.sub(r"[^a-z0-9]+", "-", str(value).casefold()).strip("-")
    return slug[:64] or None


def _ids(raw: object) -> set[int]:
    try:
        return {int(value) for value in json.loads(raw or "[]")}
    except (TypeError, ValueError, json.JSONDecodeError):
        return set()


def _steps(raw: object) -> tuple[str, ...]:
    try:
        values = json.loads(raw or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        return ()
    if not isinstance(values, list):
        return ()
    return tuple(str(value) for value in values if str(value).strip())


def _row(row: sqlite3.Row) -> QueuedSuggestion:
    return QueuedSuggestion(
        id=int(row["id"]),
        created=str(row["created"]),
        priority=int(row["priority"]),
        text=str(row["text"]),
        reason=row["reason"],
        source_event_ids=tuple(sorted(_ids(row["source_event_ids"]))),
        status=str(row["status"]),
        shown_at=row["shown_at"],
        steps=_steps(row["steps"]),
        evidence=row["evidence"],
        category=row["category"],
        confidence=row["confidence"],
        cluster=row["cluster_key"],
        duplicate_count=int(row["duplicate_count"] or 1),
    )
