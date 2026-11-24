"""Passive calendar and foreground-activity monitoring.

Ambient observations never become raw event spam. Local rules turn only a few
patterns into ordinary suggestions: a calendar event is close, a focus block is
long, context switching is high, or it is late at night. Those suggestions use
the normal queue, quiet hours, toast cap, GUI, and digest.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from ..contracts.outputs import Suggestion
from .activity import FOCUS_CATEGORIES, Activity, snapshot
from .ambient_config import AmbientConfig
from .calendar import CalendarEvent, read_ics

logger = logging.getLogger(__name__)


class AmbientMonitor:
    def __init__(
        self,
        conn: sqlite3.Connection,
        config: AmbientConfig,
        queue,
        calendar_file: Path | None,
        *,
        activity: Callable[[], Activity | None] = snapshot,
        sanitize: Callable[[str], str] | None = None,
    ) -> None:
        self.conn = conn
        self.config = config
        self.queue = queue
        self.calendar_file = calendar_file
        self.activity = activity
        self.sanitize = sanitize
        self._focus_key: str | None = None
        self._focus_since: datetime | None = None
        self._focus_alerted = False
        self._switch_points: deque[tuple[datetime, str]] = deque()
        self._last_switch_alert: datetime | None = None
        self._late_night_date: str | None = None
        self._created: list[int] = []

    def tick(self, now: datetime | None = None) -> tuple[int, ...]:
        if not self.config.enabled:
            return ()
        self._created = []
        moment = (now or datetime.now().astimezone()).astimezone()
        if self.calendar_file is not None:
            try:
                events = read_ics(self.calendar_file, days=2, now=moment)
            except OSError:
                events = []
            self._calendar_tick(events, moment)

        current = self.activity()
        if current is not None:
            self._activity_tick(current, moment)
            self._late_night_tick(current, moment)
        return tuple(self._created)

    # -- calendar ----------------------------------------------------------

    def _calendar_tick(self, events: list[CalendarEvent], now: datetime) -> None:
        for event in events:
            delta = (event.start - now).total_seconds() / 60
            if delta > self.config.calendar_lookahead_minutes or delta < -2:
                continue
            minutes = int(round(delta))
            summary = self._clean(event.summary)
            location = self._clean(event.location)
            if minutes > 1:
                text = f"In {minutes} minutes: {summary} at {event.start:%H:%M}."
            elif minutes >= -1:
                text = f"Starting now: {summary}."
            else:
                text = f"Started {abs(minutes)} minutes ago: {summary}."
            if location:
                text += f" Location: {location}."
            key = f"{event.uid or _slug(summary)}:{event.start.isoformat()}"
            self._emit(
                kind="calendar",
                key=key,
                text=text,
                reason="Upcoming local calendar event",
                steps=["Open the calendar entry", "Wrap up or switch context"],
                evidence=f"Local calendar at {event.start:%Y-%m-%d %H:%M}",
                category="calendar",
                priority=2,
                now=now,
            )

    # -- activity ----------------------------------------------------------

    def _activity_tick(self, current: Activity, now: datetime) -> None:
        idle_limit = self.config.idle_minutes * 60
        if current.idle_seconds >= idle_limit:
            self._focus_key = None
            self._focus_since = None
            self._focus_alerted = False
            self._switch_points.clear()
            return

        key = current.category
        if key != self._focus_key:
            if self._focus_key is not None:
                self._switch_points.append((now, key))
            self._focus_key = key
            self._focus_since = now
            self._focus_alerted = False
        elif key in FOCUS_CATEGORIES and self._focus_since is not None:
            focus_for = now - self._focus_since
            if (
                not self._focus_alerted
                and focus_for >= timedelta(minutes=self.config.focus_minutes)
            ):
                label = _label(current.category)
                self._emit(
                    kind="focus",
                    key=f"{now.date()}:{self._focus_since.isoformat()}:{key}",
                    text=(
                        f"You've been focused on {label} for "
                        f"{_human(focus_for)}. A short break would help."
                    ),
                    reason="Long focus block",
                    steps=["Take a 5-minute break", "Stretch and drink water"],
                    evidence=f"Foreground app: {current.app or 'unknown'}",
                    category="wellbeing",
                    priority=2,
                    now=now,
                )
                self._focus_alerted = True

        self._switch_tick(current, now)

    def _switch_tick(self, current: Activity, now: datetime) -> None:
        window = now - timedelta(minutes=self.config.switch_window_minutes)
        while self._switch_points and self._switch_points[0][0] < window:
            self._switch_points.popleft()
        if len(self._switch_points) < self.config.switch_threshold:
            return
        if (
            self._last_switch_alert is not None
            and now - self._last_switch_alert < timedelta(hours=1)
        ):
            return
        self._emit(
            kind="switches",
            key=f"{now:%Y-%m-%dT%H}:{len(self._switch_points)}",
            text=(
                f"{len(self._switch_points)} context switches in the last "
                f"{self.config.switch_window_minutes} minutes. "
                "Want to batch similar work?"
            ),
            reason="High context switching",
            steps=["Pick one task for the next block", "Silence nonessential alerts"],
            evidence=f"Foreground app changes; current is {current.category}",
            category="wellbeing",
            priority=3,
            now=now,
        )
        self._last_switch_alert = now

    def _late_night_tick(self, current: Activity, now: datetime) -> None:
        hour = now.hour
        if self.config.late_night_hour <= 0:
            return
        if not (self.config.late_night_hour <= hour < 6):
            return
        if current.idle_seconds >= self.config.idle_minutes * 60:
            return
        day = now.date().isoformat()
        if self._late_night_date == day:
            return
        self._late_night_date = day
        self._emit(
            kind="late-night",
            key=day,
            text=(
                f"It's {now:%H:%M}. You're still active in "
                f"{_label(current.category)}. Wrapping up soon would help tomorrow."
            ),
            reason="Late-night activity",
            steps=["Save your place", "Set a stopping point"],
            evidence=f"Foreground app: {current.app or 'unknown'}",
            category="wellbeing",
            priority=3,
            now=now,
        )

    # -- queue -------------------------------------------------------------

    def _emit(
        self,
        *,
        kind: str,
        key: str,
        text: str,
        reason: str,
        steps: list[str],
        evidence: str,
        category: str,
        priority: int,
        now: datetime,
    ) -> int | None:
        if not self._under_rate_limit(now):
            return None
        created = now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        clean_text = self._clean(text)[:600]
        clean_body = self._clean(text)[:600]
        cursor = self.conn.execute(
            "INSERT OR IGNORE INTO ambient_alerts "
            "(created, kind, alert_key, title, body, suggestion_id) "
            "VALUES (?, ?, ?, ?, ?, NULL)",
            (created, kind, key, clean_text, clean_body),
        )
        if cursor.rowcount == 0:
            return None
        try:
            suggestion_id = self.queue.submit(
                Suggestion(
                    text=clean_text,
                    priority=priority,
                    reason=reason,
                    steps=tuple(steps),
                    evidence=evidence,
                    category=category,
                    confidence=0.8 if kind == "calendar" else 0.6,
                    cluster=f"ambient-{kind}-{key}",
                )
            )
            self.conn.execute(
                "UPDATE ambient_alerts SET suggestion_id = ? "
                "WHERE kind = ? AND alert_key = ?",
                (suggestion_id, kind, key),
            )
            self.conn.commit()
            self._created.append(suggestion_id)
            return suggestion_id
        except Exception:
            self.conn.execute(
                "DELETE FROM ambient_alerts WHERE kind = ? AND alert_key = ?",
                (kind, key),
            )
            self.conn.commit()
            logger.exception("ambient alert could not be queued")
            return None

    def _under_rate_limit(self, now: datetime) -> bool:
        since = (now.astimezone(timezone.utc) - timedelta(hours=1)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        count = int(
            self.conn.execute(
                "SELECT COUNT(*) FROM ambient_alerts WHERE created >= ?",
                (since,),
            ).fetchone()[0]
        )
        return count < self.config.max_alerts_per_hour

    def _clean(self, text: str) -> str:
        if self.sanitize is not None:
            return self.sanitize(text)
        return text


def _slug(value: str) -> str:
    text = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
    return text[:40] or "event"


def _label(category: str) -> str:
    return {
        "coding": "coding",
        "terminal": "the terminal",
        "writing": "writing",
        "design": "design work",
        "browsing": "browsing",
        "meeting": "a meeting",
        "chat": "messages",
        "media": "media",
        "files": "files",
        "other": "a task",
    }.get(category, "a task")


def _human(delta: timedelta) -> str:
    minutes = max(1, int(delta.total_seconds() // 60))
    if minutes < 60:
        return f"{minutes} minutes"
    hours, rest = divmod(minutes, 60)
    return f"{hours}h {rest:02d}m"
