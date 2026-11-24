from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from alice.app.calendar import read_ics


def test_read_ics_returns_upcoming_events(tmp_path: Path) -> None:
    now = datetime(2026, 11, 24, 10, 0, tzinfo=timezone.utc)
    path = tmp_path / "calendar.ics"
    path.write_text(
        "\n".join(
            [
                "BEGIN:VCALENDAR",
                "VERSION:2.0",
                "BEGIN:VEVENT",
                "UID:standup@example",
                "DTSTART:20261124T103000Z",
                "DTEND:20261124T104500Z",
                "SUMMARY:Team standup",
                "LOCATION:Room 4",
                "END:VEVENT",
                "BEGIN:VEVENT",
                "UID:past@example",
                "DTSTART:20261124T090000Z",
                "SUMMARY:Past event",
                "END:VEVENT",
                "END:VCALENDAR",
            ]
        ),
        encoding="utf-8",
    )

    events = read_ics(path, days=1, now=now)

    assert len(events) == 1
    assert events[0].summary == "Team standup"
    assert events[0].location == "Room 4"
    assert events[0].uid == "standup@example"
    assert events[0].start.astimezone(timezone.utc).hour == 10
    assert events[0].start.astimezone(timezone.utc).minute == 30


def test_read_ics_handles_folded_lines(tmp_path: Path) -> None:
    now = datetime(2026, 11, 24, 10, 0, tzinfo=timezone.utc)
    path = tmp_path / "calendar.ics"
    path.write_text(
        "BEGIN:VCALENDAR\n"
        "BEGIN:VEVENT\n"
        "DTSTART:20261124T110000Z\n"
        "SUMMARY:A long\n"
        "  title\n"
        "END:VEVENT\n"
        "END:VCALENDAR\n",
        encoding="utf-8",
    )
    events = read_ics(path, days=1, now=now)
    assert events[0].summary == "A long title"
