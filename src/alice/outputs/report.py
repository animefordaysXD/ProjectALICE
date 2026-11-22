"""Weekly usage and activity report.

The report is a plain markdown file built from the local ledger. It never calls
a model. Costs come from `usage` rows; subscription providers report zero, so
the token columns are the useful trend for OpenCode Go.
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path


@dataclass(frozen=True)
class ReportResult:
    path: Path
    created: bool
    text: str
    days: int


def _since(days: int, now: datetime) -> str:
    start = now.astimezone(timezone.utc) - timedelta(days=max(1, days) - 1)
    return start.strftime("%Y-%m-%dT00:00:00Z")


def _table(headers: list[str], rows: list[list[str]]) -> list[str]:
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    lines.extend("| " + " | ".join(row) + " |" for row in rows)
    return lines


def _money(value: float) -> str:
    return f"${value:.4f}"


def _number(value: int) -> str:
    return f"{value:,}"


def render_report(
    conn: sqlite3.Connection, *, days: int = 7, now: datetime | None = None
) -> str:
    """Build the markdown report text without writing anything."""
    days = max(1, days)
    moment = now or datetime.now(timezone.utc)
    local_day = moment.astimezone().date()
    since = _since(days, moment)

    totals = conn.execute(
        """
        SELECT COUNT(*) AS calls,
               COALESCE(SUM(input_tokens), 0) AS input_tokens,
               COALESCE(SUM(cached_tokens), 0) AS cached_tokens,
               COALESCE(SUM(output_tokens), 0) AS output_tokens,
               COALESCE(SUM(cost_usd), 0) AS cost
        FROM usage WHERE ts >= ?
        """,
        (since,),
    ).fetchone()
    calls = int(totals["calls"])
    spend = float(totals["cost"])

    daily = conn.execute(
        """
        SELECT substr(ts, 1, 10) AS day,
               COUNT(*) AS calls,
               COALESCE(SUM(input_tokens), 0) AS input_tokens,
               COALESCE(SUM(cached_tokens), 0) AS cached_tokens,
               COALESCE(SUM(output_tokens), 0) AS output_tokens,
               COALESCE(SUM(cost_usd), 0) AS cost
        FROM usage WHERE ts >= ?
        GROUP BY day ORDER BY day
        """,
        (since,),
    ).fetchall()
    tiers = conn.execute(
        """
        SELECT tier, COUNT(*) AS calls,
               COALESCE(SUM(input_tokens), 0) AS input_tokens,
               COALESCE(SUM(output_tokens), 0) AS output_tokens,
               COALESCE(SUM(cost_usd), 0) AS cost
        FROM usage WHERE ts >= ?
        GROUP BY tier ORDER BY cost DESC, calls DESC
        """,
        (since,),
    ).fetchall()
    models = conn.execute(
        """
        SELECT model, tier, COUNT(*) AS calls,
               COALESCE(SUM(input_tokens), 0) AS input_tokens,
               COALESCE(SUM(output_tokens), 0) AS output_tokens,
               COALESCE(SUM(cost_usd), 0) AS cost
        FROM usage WHERE ts >= ?
        GROUP BY model, tier ORDER BY cost DESC, calls DESC
        """,
        (since,),
    ).fetchall()
    activity = conn.execute(
        """
        SELECT day,
               SUM(events) AS events,
               SUM(suggestions) AS suggestions
        FROM (
            SELECT substr(ts, 1, 10) AS day, COUNT(*) AS events, 0 AS suggestions
            FROM events WHERE ts >= ? GROUP BY day
            UNION ALL
            SELECT substr(created, 1, 10) AS day, 0 AS events, COUNT(*) AS suggestions
            FROM suggestions WHERE created >= ? GROUP BY day
        )
        GROUP BY day ORDER BY day
        """,
        (since, since),
    ).fetchall()
    alerts = conn.execute(
        """
        SELECT ts, tier, reason FROM budget_alerts
        WHERE ts >= ? ORDER BY ts DESC LIMIT 20
        """,
        (since,),
    ).fetchall()
    pending = conn.execute(
        "SELECT COUNT(*) FROM suggestions WHERE status = 'queued'"
    ).fetchone()[0]
    proposals = conn.execute(
        "SELECT COUNT(*) FROM proposals WHERE status = 'pending'"
    ).fetchone()[0]

    start_day = (moment.astimezone(timezone.utc) - timedelta(days=days - 1)).date()
    lines = [
        f"# Alice report - {local_day.isoformat()}",
        "",
        f"Period: last {days} day(s), {start_day.isoformat()} to {local_day.isoformat()}",
        "",
        "## Totals",
        "",
        f"- Model calls: {_number(calls)}",
        f"- Spend: {_money(spend)}",
        f"- Input tokens: {_number(int(totals['input_tokens']))} "
        f"({_number(int(totals['cached_tokens']))} cached)",
        f"- Output tokens: {_number(int(totals['output_tokens']))}",
        f"- Average per call: {_money(spend / calls) if calls else _money(0)}",
        "",
        "## Daily usage",
        "",
    ]
    if daily:
        lines.extend(
            _table(
                ["Day", "Calls", "Input", "Cached", "Output", "Cost"],
                [
                    [
                        str(row["day"]),
                        _number(int(row["calls"])),
                        _number(int(row["input_tokens"])),
                        _number(int(row["cached_tokens"])),
                        _number(int(row["output_tokens"])),
                        _money(float(row["cost"])),
                    ]
                    for row in daily
                ],
            )
        )
    else:
        lines.append("_No model calls in this period._")

    lines.extend(["", "## By tier", ""])
    if tiers:
        lines.extend(
            _table(
                ["Tier", "Calls", "Input", "Output", "Cost"],
                [
                    [
                        str(row["tier"]),
                        _number(int(row["calls"])),
                        _number(int(row["input_tokens"])),
                        _number(int(row["output_tokens"])),
                        _money(float(row["cost"])),
                    ]
                    for row in tiers
                ],
            )
        )
    else:
        lines.append("_No model calls in this period._")

    lines.extend(["", "## By model", ""])
    if models:
        lines.extend(
            _table(
                ["Model", "Tier", "Calls", "Input", "Output", "Cost"],
                [
                    [
                        str(row["model"]),
                        str(row["tier"]),
                        _number(int(row["calls"])),
                        _number(int(row["input_tokens"])),
                        _number(int(row["output_tokens"])),
                        _money(float(row["cost"])),
                    ]
                    for row in models
                ],
            )
        )
    else:
        lines.append("_No model calls in this period._")

    lines.extend(["", "## Activity", ""])
    if activity:
        lines.extend(
            _table(
                ["Day", "Events", "Suggestions"],
                [
                    [
                        str(row["day"]),
                        _number(int(row["events"] or 0)),
                        _number(int(row["suggestions"] or 0)),
                    ]
                    for row in activity
                ],
            )
        )
    else:
        lines.append("_No events or suggestions in this period._")

    lines.extend(
        [
            "",
            "## Queue",
            "",
            f"- Suggestions queued now: {_number(int(pending))}",
            f"- Memory proposals pending: {_number(int(proposals))}",
            "",
            "## Budget alerts",
            "",
        ]
    )
    if alerts:
        lines.extend(
            f"- `{row['ts']}` **{row['tier']}**: {row['reason']}" for row in alerts
        )
    else:
        lines.append("_None._")
    lines.extend(
        [
            "",
            "_Read-only secretary: Alice suggests, never acts._",
            "",
        ]
    )
    return "\n".join(lines)


def write_report(
    conn: sqlite3.Connection,
    data_dir: Path | str,
    *,
    days: int = 7,
    now: datetime | None = None,
) -> ReportResult:
    moment = now or datetime.now(timezone.utc)
    text = render_report(conn, days=days, now=moment)
    directory = Path(data_dir) / "reports"
    directory.mkdir(parents=True, exist_ok=True)
    day = moment.astimezone().date().isoformat()
    path = directory / f"report-{day}.md"
    created = not path.exists()
    temp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    temp.write_text(text, encoding="utf-8")
    os.replace(temp, path)
    return ReportResult(path=path, created=created, text=text, days=max(1, days))
