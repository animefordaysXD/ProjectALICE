"""Small read-only checks for scripts/crash_recovery.ps1.

Kept as a file so the PowerShell script does not need to quote Python source.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print("usage: crash_stats.py count|integrity|cursor DB [LOG]", file=sys.stderr)
        return 2
    mode = argv[1]
    db = argv[2]
    conn = sqlite3.connect(db)
    try:
        if mode == "count":
            row = conn.execute(
                "SELECT COUNT(*) FROM events WHERE source LIKE 'logfile:%'"
            ).fetchone()
            print(int(row[0]))
        elif mode == "integrity":
            print(conn.execute("PRAGMA integrity_check").fetchone()[0])
        elif mode == "cursor":
            if len(argv) < 4:
                print("cursor mode needs a log path", file=sys.stderr)
                return 2
            row = conn.execute(
                "SELECT position FROM cursors ORDER BY source LIMIT 1"
            ).fetchone()
            offset = json.loads(row[0])["offset"] if row else 0
            print(int(offset))
            print(os.path.getsize(argv[3]))
        else:
            print(f"unknown mode: {mode}", file=sys.stderr)
            return 2
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
