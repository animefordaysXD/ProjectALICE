from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from alice.core.db import connect, init_db

ROOT = Path(__file__).resolve().parents[1]


def test_session_report_writes_handover(tmp_path: Path) -> None:
    db = tmp_path / "alice.db"
    init_db(db)
    conn = connect(db, create=False)
    try:
        conn.execute(
            "INSERT INTO events (ts, source, kind, content_hash, text_redacted, salience, status) "
            "VALUES ('2026-11-22T10:00:00Z', 'logfile:app.log', 'log', 'h1', "
            "'disk failure', 0.9, 'escalated')"
        )
        conn.execute(
            "INSERT INTO triage_batches "
            "(created, event_ids, decision, priority, reason, attempts) "
            "VALUES ('2026-11-22T10:01:00Z', '[1]', 'escalate', 1, "
            "'needs a look', 2)"
        )
        conn.execute(
            "INSERT INTO suggestions (created, priority, text, reason, status) "
            "VALUES ('2026-11-22T10:02:00Z', 1, 'check the disk', "
            "'disk warning', 'queued')"
        )
        conn.execute(
            "INSERT INTO usage (ts, tier, model, input_tokens, cached_tokens, "
            "output_tokens, cost_usd) "
            "VALUES ('2026-11-22T10:03:00Z', 'triage', 'fake-triage', 100, 10, 20, 0.01)"
        )
        conn.execute(
            "INSERT INTO budget_alerts (ts, tier, reason) "
            "VALUES ('2026-11-22T10:04:00Z', 'triage', 'soft cap reached')"
        )
        conn.commit()
    finally:
        conn.close()

    out = tmp_path / "handover.md"
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "session_report.py"),
            "--db",
            str(db),
            "--out",
            str(out),
            "--start",
            "2026-11-22T09:00:00Z",
            "--end",
            "2026-11-22T11:00:00Z",
            "--label",
            "test session",
            "--data-dir",
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    text = out.read_text(encoding="utf-8")
    assert "# Alice handover - test session" in text
    assert "Events in this session: 1" in text
    assert "Triage batches: 1" in text
    assert "Suggestions created: 1" in text
    assert "Model calls: 1" in text
    assert "priority-1 suggestions" in text
    assert "1 budget alert" in text
    assert "1 triage batch(es) needed a retry" in text
    assert "check the disk" in text
    assert "Integrity: `ok`" in text
