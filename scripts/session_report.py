"""Build a handover report for a timed Alice run.

This script is intentionally read-only. It queries the local database and the
session logs, then writes one markdown file under the handover folder.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
from pathlib import Path


def _one(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> int | float:
    row = conn.execute(sql, params).fetchone()
    return 0 if row is None or row[0] is None else row[0]


def _table(headers: list[str], rows: list[list[str]]) -> list[str]:
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    lines.extend("| " + " | ".join(row) + " |" for row in rows)
    return lines


def _short(text: str, limit: int = 180) -> str:
    clean = " ".join(str(text).split())
    return clean if len(clean) <= limit else clean[: limit - 1] + "..."


def _read_text(path: Path) -> str:
    """Read UTF-8 or the UTF-16 that Windows PowerShell redirection writes."""
    data = path.read_bytes()
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", errors="replace")
    return data.decode("utf-8-sig", errors="replace")


def _pytest_summary(path: Path | None) -> str:
    if path is None or not path.is_file():
        return "not run"
    try:
        lines = _read_text(path).splitlines()
    except OSError:
        return "unreadable"
    for line in reversed(lines):
        if re.search(r"\b(passed|failed|error)\b", line, re.I):
            return line.strip()
    return "no summary line"


def _log_summary(log_path: Path) -> tuple[int, int, list[str]]:
    """Count warnings/errors and return a few unique samples."""
    if not log_path.is_file():
        return 0, 0, []
    warnings = errors = 0
    samples: list[str] = []
    try:
        lines = _read_text(log_path).splitlines()[-5000:]
    except OSError:
        return 0, 0, []
    for line in lines:
        if " ERROR " in line:
            errors += 1
        elif " WARNING " in line:
            warnings += 1
        if (" ERROR " in line or " WARNING " in line) and len(samples) < 8:
            samples.append(_short(line, 220))
    return warnings, errors, samples


def _supervisor_summary(path: Path | None) -> tuple[str, list[list[str]]]:
    if path is None or not path.is_file():
        return "not available", []
    try:
        data = json.loads(_read_text(path))
    except (OSError, ValueError):
        return "unreadable", []
    rows = []
    for index, run in enumerate(data.get("runs", []), start=1):
        rows.append(
            [
                str(index),
                str(run.get("started", "?")),
                str(run.get("ended", "?")),
                str(run.get("reason", "?")),
            ]
        )
    return f"{len(rows)} process run(s)", rows


def build_report(
    db_path: Path,
    *,
    start: str,
    end: str,
    label: str,
    supervisor_path: Path | None,
    pytest_log: Path | None,
    data_dir: Path,
) -> str:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        schema = _one(conn, "SELECT COALESCE(MAX(version), 0) FROM schema_migrations")
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        events_total = _one(conn, "SELECT COUNT(*) FROM events")
        events_session = _one(
            conn, "SELECT COUNT(*) FROM events WHERE ts >= ? AND ts < ?", (start, end)
        )
        events_new = _one(conn, "SELECT COUNT(*) FROM events WHERE status = 'new'")
        above_gate = _one(
            conn,
            "SELECT COUNT(*) FROM events WHERE status = 'new' AND salience >= 0.5",
        )
        status_rows = conn.execute(
            "SELECT status, COUNT(*) AS n FROM events "
            "WHERE ts >= ? AND ts < ? GROUP BY status ORDER BY n DESC",
            (start, end),
        ).fetchall()
        source_rows = conn.execute(
            "SELECT source, COUNT(*) AS n FROM events "
            "WHERE ts >= ? AND ts < ? GROUP BY source ORDER BY n DESC LIMIT 15",
            (start, end),
        ).fetchall()
        triage_total = _one(
            conn,
            "SELECT COUNT(*) FROM triage_batches WHERE created >= ? AND created < ?",
            (start, end),
        )
        triage_rows = conn.execute(
            "SELECT decision, COUNT(*) AS n FROM triage_batches "
            "WHERE created >= ? AND created < ? GROUP BY decision ORDER BY n DESC",
            (start, end),
        ).fetchall()
        retries = _one(
            conn,
            "SELECT COUNT(*) FROM triage_batches WHERE created >= ? AND created < ? "
            "AND attempts > 1",
            (start, end),
        )
        suggestion_total = _one(
            conn,
            "SELECT COUNT(*) FROM suggestions WHERE created >= ? AND created < ?",
            (start, end),
        )
        suggestion_status = conn.execute(
            "SELECT status, COUNT(*) AS n FROM suggestions "
            "WHERE created >= ? AND created < ? GROUP BY status ORDER BY n DESC",
            (start, end),
        ).fetchall()
        top_suggestions = conn.execute(
            "SELECT id, priority, status, text, reason FROM suggestions "
            "WHERE created >= ? AND created < ? ORDER BY priority, id DESC LIMIT 20",
            (start, end),
        ).fetchall()
        queued = _one(conn, "SELECT COUNT(*) FROM suggestions WHERE status = 'queued'")
        usage = conn.execute(
            "SELECT COUNT(*) AS calls, "
            "COALESCE(SUM(input_tokens), 0) AS input_tokens, "
            "COALESCE(SUM(cached_tokens), 0) AS cached_tokens, "
            "COALESCE(SUM(output_tokens), 0) AS output_tokens, "
            "COALESCE(SUM(cost_usd), 0) AS cost "
            "FROM usage WHERE ts >= ? AND ts < ?",
            (start, end),
        ).fetchone()
        usage_tiers = conn.execute(
            "SELECT tier, COUNT(*) AS calls, "
            "COALESCE(SUM(input_tokens), 0) AS input_tokens, "
            "COALESCE(SUM(output_tokens), 0) AS output_tokens, "
            "COALESCE(SUM(cost_usd), 0) AS cost "
            "FROM usage WHERE ts >= ? AND ts < ? "
            "GROUP BY tier ORDER BY calls DESC",
            (start, end),
        ).fetchall()
        alerts = conn.execute(
            "SELECT ts, tier, reason FROM budget_alerts "
            "WHERE ts >= ? AND ts < ? ORDER BY ts DESC LIMIT 20",
            (start, end),
        ).fetchall()
        tasks_open = _one(conn, "SELECT COUNT(*) FROM tasks WHERE status = 'open'")
        backups = conn.execute(
            "SELECT day, path, size_bytes FROM backup_runs "
            "WHERE created >= ? AND created < ? ORDER BY day",
            (start, end),
        ).fetchall()
        consolidations = conn.execute(
            "SELECT day, summary_count, proposal_count FROM consolidation_runs "
            "WHERE created >= ? AND created < ? ORDER BY day",
            (start, end),
        ).fetchall()
        cursors = _one(conn, "SELECT COUNT(*) FROM cursors")
    finally:
        conn.close()

    warnings, errors, log_samples = _log_summary(data_dir / "logs" / "alice.log")
    run_summary, run_rows = _supervisor_summary(supervisor_path)
    pytest_line = _pytest_summary(pytest_log)

    notable: list[str] = []
    if integrity != "ok":
        notable.append(f"database integrity check: {integrity}")
    if errors:
        notable.append(f"{errors} ERROR line(s) in the Alice log")
    if any(row["priority"] == 1 for row in top_suggestions):
        notable.append("priority-1 suggestions were produced")
    if alerts:
        notable.append(f"{len(alerts)} budget alert(s)")
    if retries:
        notable.append(f"{retries} triage batch(es) needed a retry")
    if above_gate:
        notable.append(f"{above_gate} event(s) are still above the gate and untriaged")

    lines = [
        f"# Alice handover - {label}",
        "",
        f"- Session window: `{start}` to `{end}`",
        f"- Database: `{db_path}`",
        f"- Schema: v{schema}",
        f"- Integrity: `{integrity}`",
        f"- Pytest: {pytest_line}",
        f"- Supervisor: {run_summary}",
        "",
        "## What happened",
        "",
        f"- Events in this session: {events_session:,}",
        f"- Events in the ledger now: {events_total:,}",
        f"- Still new: {events_new:,}",
        f"- Still new and above gate: {above_gate:,}",
        f"- Triage batches: {triage_total:,}",
        f"- Suggestions created: {suggestion_total:,}",
        f"- Suggestions queued now: {queued:,}",
        f"- Open tasks: {tasks_open:,}",
        f"- Cursors tracked: {cursors:,}",
        f"- Model calls: {int(usage['calls']):,}",
        f"- Tokens: {int(usage['input_tokens']):,} in "
        f"({int(usage['cached_tokens']):,} cached) / "
        f"{int(usage['output_tokens']):,} out",
        f"- Cost: ${float(usage['cost']):.4f}",
        "",
    ]

    if notable:
        lines.extend(["## Notable", ""])
        lines.extend(f"- {item}" for item in notable)
        lines.append("")

    lines.extend(["## Process runs", ""])
    if run_rows:
        lines.extend(_table(["#", "Started", "Ended", "Reason"], run_rows))
    else:
        lines.append("_Supervisor metadata was not available._")
    lines.extend(["", "## Events by status", ""])
    if status_rows:
        lines.extend(
            _table(
                ["Status", "Events"],
                [[str(row["status"]), f"{int(row['n']):,}"] for row in status_rows],
            )
        )
    else:
        lines.append("_No events in this window._")

    lines.extend(["", "## Top event sources", ""])
    if source_rows:
        lines.extend(
            _table(
                ["Source", "Events"],
                [[_short(row["source"], 100), f"{int(row['n']):,}"] for row in source_rows],
            )
        )
    else:
        lines.append("_No events in this window._")

    lines.extend(["", "## Triage", ""])
    if triage_rows:
        lines.extend(
            _table(
                ["Decision", "Batches"],
                [[str(row["decision"]), f"{int(row['n']):,}"] for row in triage_rows],
            )
        )
    else:
        lines.append("_No triage batches in this window._")

    lines.extend(["", "## Suggestions", ""])
    if suggestion_status:
        lines.extend(
            _table(
                ["Status", "Suggestions"],
                [[str(row["status"]), f"{int(row['n']):,}"] for row in suggestion_status],
            )
        )
        lines.append("")
    if top_suggestions:
        for row in top_suggestions:
            reason = f" - {_short(row['reason'], 120)}" if row["reason"] else ""
            lines.append(
                f"- **P{row['priority']}** [{row['status']}] "
                f"{_short(row['text'])}{reason}"
            )
    else:
        lines.append("_No suggestions created in this window._")

    lines.extend(["", "## Usage by tier", ""])
    if usage_tiers:
        lines.extend(
            _table(
                ["Tier", "Calls", "Input", "Output", "Cost"],
                [
                    [
                        str(row["tier"]),
                        f"{int(row['calls']):,}",
                        f"{int(row['input_tokens']):,}",
                        f"{int(row['output_tokens']):,}",
                        f"${float(row['cost']):.4f}",
                    ]
                    for row in usage_tiers
                ],
            )
        )
    else:
        lines.append("_No model calls in this window._")

    lines.extend(["", "## Budget alerts", ""])
    if alerts:
        lines.extend(
            f"- `{row['ts']}` **{row['tier']}**: {_short(row['reason'], 220)}"
            for row in alerts
        )
    else:
        lines.append("_None._")

    lines.extend(["", "## Consolidation and backups", ""])
    if consolidations:
        lines.extend(
            f"- Consolidated `{row['day']}`: {row['summary_count']} summaries, "
            f"{row['proposal_count']} proposals"
            for row in consolidations
        )
    if backups:
        lines.extend(
            f"- Backup `{row['day']}`: `{row['path']}` ({int(row['size_bytes']):,} bytes)"
            for row in backups
        )
    if not consolidations and not backups:
        lines.append("_None in this window._")

    lines.extend(["", "## Alice log", ""])
    lines.append(f"- Warnings: {warnings:,}")
    lines.append(f"- Errors: {errors:,}")
    if log_samples:
        lines.append("")
        lines.extend(f"- `{_short(sample, 220)}`" for sample in log_samples)

    lines.extend(
        [
            "",
            "_Alice is read-only and suggest-only. No suggestion in this report was executed._",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--start", required=True, help="ISO UTC, e.g. 2026-11-22T08:00:00Z")
    parser.add_argument("--end", required=True)
    parser.add_argument("--label", default="session")
    parser.add_argument("--supervisor", default=None)
    parser.add_argument("--pytest-log", default=None)
    parser.add_argument("--data-dir", required=True)
    args = parser.parse_args()

    text = build_report(
        Path(args.db),
        start=args.start,
        end=args.end,
        label=args.label,
        supervisor_path=Path(args.supervisor) if args.supervisor else None,
        pytest_log=Path(args.pytest_log) if args.pytest_log else None,
        data_dir=Path(args.data_dir),
    )
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
