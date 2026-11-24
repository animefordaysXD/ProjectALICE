from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from alice.app.ambient import AmbientMonitor
from alice.app.ambient_config import AmbientConfig
from alice.app.activity import Activity
from alice.core.db import connect, init_db
from alice.outputs.queue import SuggestionQueue


@pytest.fixture
def conn(tmp_path: Path):
    db = tmp_path / "alice.db"
    init_db(db)
    connection = connect(db, create=False)
    yield connection
    connection.close()


def activity(category: str, app: str = "code.exe", idle: int = 0) -> Activity:
    return Activity(
        app=app,
        title="notes.py - editor",
        category=category,
        idle_seconds=idle,
        captured_at="2026-11-24T14:00:00+09:00",
    )


def config(**kwargs) -> AmbientConfig:
    values = {
        "calendar_lookahead_minutes": 30,
        "focus_minutes": 10,
        "idle_minutes": 30,
        "switch_window_minutes": 30,
        "switch_threshold": 5,
        "late_night_hour": 0,
        "max_alerts_per_hour": 2,
    }
    values.update(kwargs)
    return AmbientConfig(**values)


def write_event(path: Path, start: datetime, summary: str = "Design review") -> None:
    path.write_text(
        "BEGIN:VCALENDAR\n"
        "BEGIN:VEVENT\n"
        "UID:review@example\n"
        f"DTSTART:{start:%Y%m%dT%H%M%S}\n"
        f"SUMMARY:{summary}\n"
        "END:VEVENT\n"
        "END:VCALENDAR\n",
        encoding="utf-8",
    )


def test_calendar_alert_is_queued_once(conn, tmp_path: Path) -> None:
    now = datetime(2026, 11, 24, 14, 0).astimezone()
    calendar = tmp_path / "calendar.ics"
    write_event(calendar, now + timedelta(minutes=10))
    queue = SuggestionQueue(conn)
    monitor = AmbientMonitor(
        conn,
        config(),
        queue,
        calendar,
        activity=lambda: None,
    )

    first = monitor.tick(now)
    second = monitor.tick(now + timedelta(minutes=1))

    assert len(first) == 1
    assert second == ()
    pending = queue.pending()
    assert len(pending) == 1
    assert pending[0].category == "calendar"
    assert "Design review" in pending[0].text
    assert "10 minutes" in pending[0].text


def test_focus_block_creates_feedback(conn) -> None:
    now = datetime(2026, 11, 24, 14, 0).astimezone()
    queue = SuggestionQueue(conn)
    monitor = AmbientMonitor(
        conn,
        config(),
        queue,
        None,
        activity=lambda: activity("coding"),
    )

    assert monitor.tick(now) == ()
    ids = monitor.tick(now + timedelta(minutes=11))

    assert len(ids) == 1
    pending = queue.pending()
    assert pending[0].category == "wellbeing"
    assert "focused on coding" in pending[0].text
    assert "10 minutes" in pending[0].text or "11 minutes" in pending[0].text


def test_context_switching_creates_feedback(conn) -> None:
    now = datetime(2026, 11, 24, 14, 0).astimezone()
    categories = ["coding", "browsing", "coding", "browsing", "coding", "browsing"]
    queue = SuggestionQueue(conn)
    monitor = AmbientMonitor(
        conn,
        config(focus_minutes=90),
        queue,
        None,
        activity=lambda: activity(categories.pop(0)),
    )

    ids = ()
    for index in range(6):
        ids = monitor.tick(now + timedelta(minutes=index))

    assert ids
    pending = queue.pending()
    assert any("context switches" in item.text for item in pending)


def test_idle_time_suppresses_focus(conn) -> None:
    now = datetime(2026, 11, 24, 14, 0).astimezone()
    queue = SuggestionQueue(conn)
    monitor = AmbientMonitor(
        conn,
        config(),
        queue,
        None,
        activity=lambda: activity("coding", idle=31 * 60),
    )

    assert monitor.tick(now) == ()
    assert monitor.tick(now + timedelta(minutes=20)) == ()
    assert queue.pending() == []


def test_rate_limit_caps_ambient_alerts(conn, tmp_path: Path) -> None:
    now = datetime(2026, 11, 24, 14, 0).astimezone()
    calendar = tmp_path / "calendar.ics"
    write_event(calendar, now + timedelta(minutes=5))
    queue = SuggestionQueue(conn)
    monitor = AmbientMonitor(
        conn,
        config(max_alerts_per_hour=1),
        queue,
        calendar,
        activity=lambda: activity("coding"),
    )

    first = monitor.tick(now)
    second = monitor.tick(now + timedelta(minutes=15))

    assert len(first) == 1
    assert second == ()
    assert len(queue.pending()) == 1
