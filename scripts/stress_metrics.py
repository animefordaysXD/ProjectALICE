"""Append one stress-session metric row to a CSV.

Read-only against Alice's database. If the database is briefly locked, the row
records the error instead of failing the stress controller.
"""

from __future__ import annotations

import argparse
import csv
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

FIELDS = [
    "ts",
    "events",
    "new",
    "above_gate",
    "triage_batches",
    "suggestions",
    "usage_calls",
    "cost",
    "cursors",
    "db_mb",
    "wal_mb",
    "rss_mb",
    "alice_processes",
    "integrity",
    "error",
]


def _one(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> int:
    row = conn.execute(sql, params).fetchone()
    return 0 if row is None or row[0] is None else int(row[0])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--csv", required=True)
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--rss-mb", type=float, default=0.0)
    parser.add_argument("--processes", type=int, default=0)
    parser.add_argument("--skip-integrity", action="store_true")
    args = parser.parse_args()

    db = Path(args.db)
    data_dir = Path(args.data_dir)
    row = {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "events": 0,
        "new": 0,
        "above_gate": 0,
        "triage_batches": 0,
        "suggestions": 0,
        "usage_calls": 0,
        "cost": 0.0,
        "cursors": 0,
        "db_mb": round(db.stat().st_size / 1048576, 2) if db.is_file() else 0.0,
        "wal_mb": 0.0,
        "rss_mb": round(args.rss_mb, 1),
        "alice_processes": args.processes,
        "integrity": "",
        "error": "",
    }
    wal = db.with_name(db.name + "-wal")
    if wal.is_file():
        row["wal_mb"] = round(wal.stat().st_size / 1048576, 2)

    try:
        conn = sqlite3.connect(str(db), timeout=10.0)
        conn.execute("PRAGMA busy_timeout=10000")
        try:
            row["events"] = _one(conn, "SELECT COUNT(*) FROM events")
            row["new"] = _one(conn, "SELECT COUNT(*) FROM events WHERE status = 'new'")
            row["above_gate"] = _one(
                conn,
                "SELECT COUNT(*) FROM events WHERE status = 'new' AND salience >= 0.5",
            )
            row["triage_batches"] = _one(conn, "SELECT COUNT(*) FROM triage_batches")
            row["suggestions"] = _one(conn, "SELECT COUNT(*) FROM suggestions")
            row["usage_calls"] = _one(conn, "SELECT COUNT(*) FROM usage")
            spend = conn.execute("SELECT COALESCE(SUM(cost_usd), 0) FROM usage").fetchone()[0]
            row["cost"] = round(float(spend or 0.0), 4)
            row["cursors"] = _one(conn, "SELECT COUNT(*) FROM cursors")
            if not args.skip_integrity:
                check = conn.execute("PRAGMA integrity_check").fetchone()
                row["integrity"] = str(check[0]) if check else "missing"
        finally:
            conn.close()
    except (sqlite3.Error, OSError) as exc:
        row["error"] = f"{type(exc).__name__}: {exc}"

    path = Path(args.csv)
    path.parent.mkdir(parents=True, exist_ok=True)
    exists = path.is_file() and path.stat().st_size > 0
    with path.open("a", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        if not exists:
            writer.writeheader()
        writer.writerow(row)

    print(
        f"{row['ts']} events={row['events']} above={row['above_gate']} "
        f"db={row['db_mb']}MB wal={row['wal_mb']}MB error={row['error'] or '-'}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
