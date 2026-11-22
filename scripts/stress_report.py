"""Summarize a stress-session metrics CSV into a markdown report."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path


def _num(row: dict, key: str) -> float:
    try:
        return float(row.get(key) or 0)
    except (TypeError, ValueError):
        return 0.0


def _restarts(session_path: Path | None) -> tuple[int, list[str]]:
    if session_path is None or not session_path.is_file():
        return 0, []
    try:
        data = json.loads(session_path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return 0, []
    reasons: list[str] = []
    for run in data.get("runs", []):
        reason = str(run.get("reason", ""))
        if "stopped at session end" not in reason:
            reasons.append(reason)
    return len(reasons), reasons


def _table(headers: list[str], rows: list[list[str]]) -> list[str]:
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    lines.extend("| " + " | ".join(row) + " |" for row in rows)
    return lines


def build(csv_path: Path, session_path: Path | None, handover: str | None) -> str:
    with csv_path.open("r", encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        return "# Alice stress summary\n\n_No metric samples were recorded._\n"

    peak_above = max(rows, key=lambda row: _num(row, "above_gate"))
    peak_wal = max(rows, key=lambda row: _num(row, "wal_mb"))
    peak_db = max(rows, key=lambda row: _num(row, "db_mb"))
    peak_rss = max(rows, key=lambda row: _num(row, "rss_mb"))
    peak_processes = max(rows, key=lambda row: _num(row, "alice_processes"))
    final = rows[-1]
    restarts, restart_reasons = _restarts(session_path)
    integrity_bad = [
        row for row in rows if row.get("integrity") and row["integrity"].lower() != "ok"
    ]
    errors = [row for row in rows if row.get("error")]

    notable: list[str] = []
    if integrity_bad:
        notable.append(f"{len(integrity_bad)} integrity-check failure(s)")
    if errors:
        notable.append(f"{len(errors)} metric sample(s) hit a database error")
    if restarts:
        notable.append(f"{restarts} process restart(s): {', '.join(restart_reasons)}")
    if _num(peak_wal, "wal_mb") >= 50:
        notable.append(f"WAL peaked at {_num(peak_wal, 'wal_mb'):.1f} MB")
    if _num(rows[-1], "above_gate") > 0:
        notable.append(f"{int(_num(rows[-1], 'above_gate')):,} above-gate events remain")
    if not notable:
        notable.append("no integrity failures, restart loops, or lock errors")

    lines = [
        "# Alice stress summary",
        "",
        f"- Samples: {len(rows):,}",
        f"- Window: `{rows[0]['ts']}` to `{rows[-1]['ts']}`",
        f"- Restarts: {restarts}",
        f"- Final events: {int(_num(final, 'events')):,}",
        f"- Final above gate: {int(_num(final, 'above_gate')):,}",
        f"- Final triage batches: {int(_num(final, 'triage_batches')):,}",
        f"- Final suggestions: {int(_num(final, 'suggestions')):,}",
        f"- Final model calls: {int(_num(final, 'usage_calls')):,}",
        f"- Final cost: ${_num(final, 'cost'):.4f}",
        f"- Final DB: {_num(final, 'db_mb'):.1f} MB "
        f"(WAL {_num(final, 'wal_mb'):.1f} MB)",
        f"- Handover: `{handover}`" if handover else "",
        "",
        "## Peaks",
        "",
        f"- Events: {int(_num(final, 'events')):,}",
        f"- Above-gate backlog: {int(_num(peak_above, 'above_gate')):,} "
        f"at `{peak_above['ts']}`",
        f"- DB size: {_num(peak_db, 'db_mb'):.1f} MB",
        f"- WAL size: {_num(peak_wal, 'wal_mb'):.1f} MB at `{peak_wal['ts']}`",
        f"- Alice RSS: {_num(peak_rss, 'rss_mb'):.1f} MB",
        f"- Alice processes: {int(_num(peak_processes, 'alice_processes'))}",
        "",
        "## Notable",
        "",
    ]
    lines.extend(f"- {item}" for item in notable)
    lines.extend(["", "## Time series", ""])
    step = max(1, len(rows) // 24)
    sample = rows[::step]
    lines.extend(
        _table(
            ["Time", "Events", "Above gate", "Triage", "DB MB", "WAL MB", "RSS MB"],
            [
                [
                    row["ts"],
                    f"{int(_num(row, 'events')):,}",
                    f"{int(_num(row, 'above_gate')):,}",
                    f"{int(_num(row, 'triage_batches')):,}",
                    f"{_num(row, 'db_mb'):.1f}",
                    f"{_num(row, 'wal_mb'):.1f}",
                    f"{_num(row, 'rss_mb'):.1f}",
                ]
                for row in sample
            ],
        )
    )
    if errors:
        lines.extend(["", "## Database errors", ""])
        for row in errors[:20]:
            lines.append(f"- `{row['ts']}` {row['error']}")
    lines.extend(
        [
            "",
            "_Stress data was generated locally; Alice remains read-only and suggest-only._",
            "",
        ]
    )
    return "\n".join(line for line in lines if line is not None)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--csv", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--session-json", default=None)
    parser.add_argument("--handover", default=None)
    args = parser.parse_args()

    text = build(
        Path(args.csv),
        Path(args.session_json) if args.session_json else None,
        args.handover,
    )
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
