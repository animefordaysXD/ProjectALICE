from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from alice.core.db import connect, init_db
from alice.outputs.report import render_report, write_report

NOW = datetime(2026, 11, 22, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def conn(tmp_path: Path):
    db = tmp_path / "alice.db"
    init_db(db)
    connection = connect(db, create=False)
    yield connection
    connection.close()


def add_usage(conn, ts: str, tier: str, model: str, input_tokens: int, output: int, cost: float):
    conn.execute(
        "INSERT INTO usage (ts, tier, model, input_tokens, cached_tokens, output_tokens, cost_usd) "
        "VALUES (?, ?, ?, ?, 0, ?, ?)",
        (ts, tier, model, input_tokens, output, cost),
    )


def test_report_totals_daily_and_tiers(conn) -> None:
    add_usage(conn, "2026-11-20T10:00:00Z", "triage", "fake-triage", 100, 20, 0.25)
    add_usage(conn, "2026-11-20T11:00:00Z", "reason", "fake-reason", 200, 50, 0.75)
    add_usage(conn, "2026-11-21T09:00:00Z", "triage", "fake-triage", 50, 10, 0.10)
    add_usage(conn, "2026-11-10T09:00:00Z", "heavy", "fake-heavy", 900, 90, 9.00)
    conn.execute(
        "INSERT INTO events (ts, source, kind, content_hash, text_redacted) "
        "VALUES ('2026-11-20T10:00:00Z', 'logfile:app.log', 'log', 'h1', 'disk')"
    )
    conn.execute(
        "INSERT INTO suggestions (created, priority, text) VALUES "
        "('2026-11-20T10:30:00Z', 2, 'check disk')"
    )
    conn.commit()

    text = render_report(conn, days=7, now=NOW)

    assert "# Alice report - 2026-11-22" in text
    assert "2026-11-16 to 2026-11-22" in text
    assert "- Model calls: 3" in text
    assert "- Spend: $1.1000" in text
    assert "- Input tokens: 350 (0 cached)" in text
    assert "- Output tokens: 80" in text
    assert "| 2026-11-20 | 2 | 300 | 0 | 70 | $1.0000 |" in text
    assert "| triage | 2 | 150 | 30 | $0.3500 |" in text
    assert "| fake-reason | reason | 1 | 200 | 50 | $0.7500 |" in text
    assert "| 2026-11-20 | 1 | 1 |" in text
    assert "fake-heavy" not in text


def test_empty_report(conn) -> None:
    text = render_report(conn, days=7, now=NOW)

    assert "_No model calls in this period._" in text
    assert "- Model calls: 0" in text
    assert "_None._" in text


def test_report_writes_markdown(conn, tmp_path: Path) -> None:
    result = write_report(conn, tmp_path / "data", days=7, now=NOW)

    assert result.created is True
    assert result.path == tmp_path / "data" / "reports" / "report-2026-11-22.md"
    assert result.path.is_file()
    assert "Alice report" in result.path.read_text(encoding="utf-8")
    second = write_report(conn, tmp_path / "data", days=7, now=NOW)
    assert second.created is False
