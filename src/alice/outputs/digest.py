"""Daily markdown digest and one summary toast."""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, time, timezone
from pathlib import Path
from typing import Callable

from ..contracts.outputs import BudgetSnapshot, Notifier
from .config import Notify
from .queue import is_quiet


@dataclass(frozen=True)
class DigestResult:
    path: Path
    created: bool
    summary: str
    notified: bool


class DigestWriter:
    def __init__(
        self,
        conn: sqlite3.Connection,
        data_dir: Path | str,
        notify: Notify,
        budget: Callable[[], BudgetSnapshot],
    ) -> None:
        self.conn = conn
        self.dir = Path(data_dir) / "digests"
        self.notify = notify
        self.budget = budget

    def write(
        self,
        *,
        now: datetime | None = None,
        notifier: Notifier | None = None,
    ) -> DigestResult:
        moment = now or datetime.now(timezone.utc)
        local = moment.astimezone()
        day = local.date()
        start = (
            datetime.combine(day, time.min, tzinfo=local.tzinfo)
            .astimezone(timezone.utc)
            .strftime("%Y-%m-%dT%H:%M:%SZ")
        )
        path = self.dir / f"{day.isoformat()}.md"
        created = not path.exists()

        snapshot = self.budget()
        suggestions = self.conn.execute(
            """
            SELECT * FROM suggestions
            WHERE created >= ? AND status != 'merged'
            ORDER BY priority, created
            """,
            (start,),
        ).fetchall()
        queued = sum(1 for row in suggestions if row["status"] == "queued")
        statuses = {
            str(row["status"]): int(row["n"])
            for row in self.conn.execute(
                "SELECT status, COUNT(*) AS n FROM events GROUP BY status"
            )
        }
        batches = self.conn.execute(
            "SELECT COUNT(*) FROM triage_batches"
        ).fetchone()[0]
        reasoned = self.conn.execute(
            "SELECT COUNT(*) FROM triage_batches WHERE reasoned_at IS NOT NULL"
        ).fetchone()[0]
        proposals = self.conn.execute(
            "SELECT COUNT(*) FROM proposals WHERE status = 'pending'"
        ).fetchone()[0]

        lines = [
            f"# Alice digest — {day.isoformat()}",
            "",
            f"Generated: {moment.astimezone().strftime('%Y-%m-%d %H:%M %Z')}",
            "",
            "## Activity",
            "",
            f"- Events: {sum(statuses.values())} total",
        ]
        for status in ("new", "triaged", "escalated", "done"):
            if statuses.get(status):
                lines.append(f"  - {status}: {statuses[status]}")
        lines.extend(
            [
                f"- Triage batches: {batches} ({reasoned} reasoned)",
                "",
                "## Suggestions",
                "",
            ]
        )
        if not suggestions:
            lines.append("No suggestions today.")
        else:
            for row in suggestions:
                reason = f" — {row['reason']}" if row["reason"] else ""
                meta = []
                if row["category"]:
                    meta.append(str(row["category"]))
                if row["confidence"] is not None:
                    meta.append(f"confidence {float(row['confidence']):.2f}")
                meta_text = f" ({', '.join(meta)})" if meta else ""
                merged = (
                    f" (merged ×{row['duplicate_count']})"
                    if int(row["duplicate_count"] or 1) > 1
                    else ""
                )
                lines.append(
                    f"- **[P{row['priority']}]** {row['text']}{reason} "
                    f"(`{row['status']}`){meta_text}{merged}"
                )
                try:
                    steps = json.loads(row["steps"] or "[]")
                except (TypeError, ValueError, json.JSONDecodeError):
                    steps = []
                for index, step in enumerate(steps, start=1):
                    lines.append(f"  {index}. {step}")
                if row["evidence"]:
                    lines.append(f"  - evidence: {row['evidence']}")
        lines.extend(
            [
                "",
                "## Budget",
                "",
                f"- Spend today: ${snapshot.spend_today:.4f} "
                f"(soft ${snapshot.soft_cap:.2f}, hard ${snapshot.hard_cap:.2f})",
                f"- Calls today: {snapshot.calls_today}",
                f"- Paused: {snapshot.paused or 'no'}",
            ]
        )
        if snapshot.alerts:
            lines.append("- Alerts:")
            lines.extend(f"  - {alert}" for alert in snapshot.alerts)
        else:
            lines.append("- Alerts: none")
        lines.extend(
            [
                "",
                "## Memory",
                "",
                f"- Pending proposals: {proposals}",
                "",
                "_Read-only secretary: Alice suggests, never acts._",
                "",
            ]
        )

        self.dir.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        temp.write_text("\n".join(lines), encoding="utf-8")
        os.replace(temp, path)

        summary = f"{queued} suggestion(s) queued, ${snapshot.spend_today:.2f} spent today"
        notified = False
        if (
            created
            and notifier is not None
            and "toast" in self.notify.mode
            and not is_quiet(local, self.notify.quiet_hours)
        ):
            notified = notifier.notify("Alice daily digest", summary)
        return DigestResult(path=path, created=created, summary=summary, notified=notified)
