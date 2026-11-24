"""Read-only local iCalendar support for tools and ambient monitoring."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path


@dataclass(frozen=True)
class CalendarEvent:
    start: datetime
    end: datetime | None
    summary: str
    location: str = ""
    uid: str = ""


def read_ics(
    path: Path,
    days: int = 7,
    *,
    now: datetime | None = None,
) -> list[CalendarEvent]:
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"\n[ \t]", "", text)
    moment = now or datetime.now().astimezone()
    if moment.tzinfo is None:
        moment = moment.astimezone()
    end = moment + timedelta(days=max(1, min(days, 365)))
    events: list[CalendarEvent] = []
    for block in re.findall(r"BEGIN:VEVENT(.*?)END:VEVENT", text, re.S):
        fields: dict[str, str] = {}
        for line in block.splitlines():
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            key = key.split(";", 1)[0].upper()
            fields[key] = value.strip()
        start = parse_ics_datetime(fields.get("DTSTART", ""))
        if start is None or start < moment - timedelta(minutes=5) or start > end:
            continue
        events.append(
            CalendarEvent(
                start=start,
                end=parse_ics_datetime(fields.get("DTEND", "")),
                summary=fields.get("SUMMARY", "(untitled)"),
                location=fields.get("LOCATION", ""),
                uid=fields.get("UID", ""),
            )
        )
    return sorted(events, key=lambda event: event.start)


def parse_ics_datetime(value: str) -> datetime | None:
    if not value:
        return None
    value = value.strip()
    for fmt in ("%Y%m%dT%H%M%SZ", "%Y%m%dT%H%M%S", "%Y%m%dT%H%MZ", "%Y%m%d"):
        try:
            parsed = datetime.strptime(value, fmt)
            if fmt.endswith("Z"):
                return parsed.replace(tzinfo=timezone.utc).astimezone()
            return parsed.astimezone()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(value).astimezone()
    except ValueError:
        return None
