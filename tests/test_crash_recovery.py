"""Crash recovery: ledger writes survive a crash before the cursor is saved.

`poll_once` writes each event (committing per event) and only then advances the
sensor cursor. If the process dies between those two steps, the next start
re-reads the same lines. Content-hash dedupe must drop them, so nothing is lost
and nothing is duplicated.
"""

from __future__ import annotations

from pathlib import Path

from alice.core.db import connect, init_db
from alice.core.events import CursorStore, RawStore, WriteKind
from alice.inputs.config import GateConfig, RedactionConfig, Source
from alice.inputs.gate import Gate
from alice.inputs.sensors.logfile import LogfileSensor


def build_sensor(log: Path, conn, *, max_lines: int) -> LogfileSensor:
    source = Source(
        type="logfile",
        options={"path": str(log), "glob": "*.log", "from_start": True, "max_lines": max_lines},
    )
    return LogfileSensor(source, root=log.parent, cursors=CursorStore(conn))


def build_gate(conn, tmp_path: Path) -> Gate:
    return Gate(
        conn,
        gate=GateConfig(),
        redaction=RedactionConfig(patterns=()),
        raw_store=RawStore(tmp_path / "raw"),
    )


def test_crash_before_cursor_commit_is_idempotent(tmp_path: Path) -> None:
    log = tmp_path / "app.log"
    log.write_text(
        "\n".join(
            [
                "alpha deploy failed",
                "bravo disk warning",
                "charlie request timeout",
                "delta queue stalled",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    db = tmp_path / "alice.db"
    init_db(db)
    conn = connect(db, create=False)
    try:
        gate = build_gate(conn, tmp_path)

        # First poll writes 2 events, then the process "dies" before commit().
        crashed = build_sensor(log, conn, max_lines=2)
        raw = crashed.poll()
        assert len(raw) == 2
        gate.ingest_many(raw)
        crashed.rollback()
        assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM cursors").fetchone()[0] == 0

        # Restart: the same two lines are re-read and deduped, not inserted.
        restarted = build_sensor(log, conn, max_lines=2)
        again = restarted.poll()
        results = gate.ingest_many(again)
        assert all(
            result.write.kind in (WriteKind.DUPLICATE, WriteKind.COLLAPSED)
            for result in results
        )
        assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 2
        restarted.commit()

        # Continue normally through the rest of the file.
        forward = build_sensor(log, conn, max_lines=2)
        for _ in range(3):
            events = forward.poll()
            if not events:
                break
            gate.ingest_many(events)
            forward.commit()

        rows = conn.execute("SELECT text_redacted FROM events ORDER BY id").fetchall()
        texts = [row[0] for row in rows]
        assert len(texts) == 4
        assert len(set(texts)) == 4
        assert texts[0].startswith("alpha")
        assert texts[-1].startswith("delta")

        # A full restart now finds nothing new.
        final = build_sensor(log, conn, max_lines=10)
        assert final.poll() == []
        final.commit()
        assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 4
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        conn.close()
