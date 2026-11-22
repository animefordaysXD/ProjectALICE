from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from alice.inputs.config import GateConfig, RedactionConfig, SalienceRule
from alice.core.db import connect, init_db
from alice.core.events import RawEvent, RawStore, WriteKind
from alice.inputs.gate import Gate, normalize

NOW = datetime(2026, 11, 21, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def setup(tmp_path: Path):
    db_path = tmp_path / "alice.db"
    init_db(db_path)
    conn = connect(db_path, create=False)
    raw_root = tmp_path / "raw"
    gate = Gate(
        conn,
        gate=GateConfig(),
        redaction=RedactionConfig(),
        raw_store=RawStore(raw_root),
    )
    yield conn, gate, raw_root
    conn.close()


def raw_event(
    text: str,
    *,
    source: str = "logfile:app.log",
    kind: str = "log",
    ts: str = "2026-11-21T11:59:00Z",
) -> RawEvent:
    return RawEvent(source=source, kind=kind, text=text, ts=ts)


def rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM events ORDER BY id").fetchall()


def test_normalize_collapses_variable_parts() -> None:
    first = normalize("2026-11-21T10:00:00Z ERROR txn 99881 id=3f2a9c1b")
    second = normalize("2026-11-21T10:05:03Z ERROR txn 99882 id=4a1b8d2e")

    assert first == second
    assert normalize("ERROR txn 1 failed") != normalize("ERROR txn 1 retried")


def test_exact_duplicate_is_dropped(setup) -> None:
    conn, gate, _ = setup

    first = gate.ingest(raw_event("ERROR disk full"), now=NOW)
    second = gate.ingest(raw_event("ERROR disk full"), now=NOW)

    assert first is not None and second is not None
    assert first.write.kind is WriteKind.INSERTED
    assert second.write.kind is WriteKind.DUPLICATE
    assert second.write.event_id == first.write.event_id
    assert len(rows(conn)) == 1
    assert rows(conn)[0]["count"] == 1


def test_near_identical_lines_collapse_with_count(setup) -> None:
    conn, gate, _ = setup

    first = gate.ingest(raw_event("2026-11-21T10:00:00Z ERROR job 1001 failed"), now=NOW)
    second = gate.ingest(raw_event("2026-11-21T10:05:00Z ERROR job 1002 failed"), now=NOW)

    assert first.write.kind is WriteKind.INSERTED
    assert second.write.kind is WriteKind.COLLAPSED
    assert second.write.count == 2
    stored = rows(conn)
    assert len(stored) == 1
    assert stored[0]["count"] == 2
    assert stored[0]["last_seen"] == "2026-11-21T12:00:00Z"
    assert "job 1001" in stored[0]["text_redacted"]


def test_duplicate_refreshes_last_seen_without_raising_count(setup) -> None:
    conn, gate, _ = setup

    gate.ingest(raw_event("routine line"), now=NOW - timedelta(hours=2))
    later = NOW + timedelta(hours=1)
    gate.ingest(raw_event("routine line"), now=later)

    stored = rows(conn)
    assert len(stored) == 1
    assert stored[0]["count"] == 1
    assert stored[0]["last_seen"] == "2026-11-21T13:00:00Z"


def test_novelty_window(setup) -> None:
    _, gate, _ = setup

    first = gate.ingest(raw_event("nightly job finished"), now=NOW - timedelta(days=2))
    repeat = gate.ingest(raw_event("nightly job finished"), now=NOW)

    assert first is not None and repeat is not None
    assert first.novel is True
    assert repeat.novel is True


def test_repeat_inside_window_is_not_novel(setup) -> None:
    _, gate, _ = setup

    first = gate.ingest(raw_event("routine line"), now=NOW)
    second = gate.ingest(raw_event("routine line"), now=NOW)

    assert first is not None and second is not None
    assert first.novel is True
    assert second.novel is False
    assert second.salience < first.salience


def test_salience_scores_keywords_and_korean(setup) -> None:
    _, gate, _ = setup

    error = gate.evaluate("payment failed", source_type="logfile", novel=False)
    routine = gate.evaluate("service started", source_type="logfile", novel=False)
    korean = gate.evaluate("배포 실패", source_type="logfile", novel=False)

    assert "error_en" in error.matched_rules
    assert error.score > routine.score
    assert "error_ko" in korean.matched_rules
    assert korean.score == error.score


def test_source_weight_raises_message_salience(setup) -> None:
    _, gate, _ = setup

    logfile = gate.evaluate("hello", source_type="logfile", novel=False)
    message = gate.evaluate("hello", source_type="message", novel=False)

    assert message.score > logfile.score


def test_gate_threshold_flags(setup) -> None:
    _, gate, _ = setup

    routine = gate.ingest(raw_event("service started"), now=NOW)
    urgent = gate.ingest(raw_event("payment failed"), now=NOW)

    assert routine is not None and urgent is not None
    assert routine.passes is False
    assert urgent.passes is True


def test_redaction_happens_before_storage_and_raw_is_kept(setup) -> None:
    conn, gate, raw_root = setup

    result = gate.ingest(
        raw_event("user jane.doe@example.com card 4111-1111-1111-1111"), now=NOW
    )

    assert result is not None
    assert "[EMAIL]" in result.text_redacted
    assert "[CARD]" in result.text_redacted
    assert result.redactions["EMAIL"] == 1
    assert result.raw_ref is not None

    stored = rows(conn)[0]
    assert stored["text_redacted"] == result.text_redacted
    assert stored["raw_ref"] == result.raw_ref
    raw_file = raw_root / result.raw_ref
    assert "jane.doe@example.com" in raw_file.read_text(encoding="utf-8")


def test_clean_event_gets_no_raw_payload(setup) -> None:
    conn, gate, raw_root = setup

    result = gate.ingest(raw_event("service started cleanly"), now=NOW)

    assert result is not None
    assert result.raw_ref is None
    assert rows(conn)[0]["raw_ref"] is None
    assert not any(raw_root.rglob("*.txt"))


def test_oversize_raw_payload_is_skipped(tmp_path: Path) -> None:
    db_path = tmp_path / "alice.db"
    init_db(db_path)
    conn = connect(db_path, create=False)
    try:
        gate = Gate(
            conn,
            gate=GateConfig(),
            redaction=RedactionConfig(),
            raw_store=RawStore(tmp_path / "raw", max_bytes=8),
        )

        result = gate.ingest(raw_event("mail jane@example.com now"), now=NOW)

        assert result is not None
        assert result.raw_ref is None
        assert not any((tmp_path / "raw").rglob("*.txt"))
    finally:
        conn.close()


def test_blank_observation_is_ignored(setup) -> None:
    conn, gate, _ = setup

    assert gate.ingest(raw_event("   \n\t "), now=NOW) is None
    assert rows(conn) == []


def test_custom_rules_replace_defaults(setup, tmp_path: Path) -> None:
    conn, _, _ = setup
    config = GateConfig(
        min_salience=0.4,
        rules=(
            SalienceRule(name="deploy", pattern=re.compile("deploy"), weight=0.4),
        ),
    )
    gate = Gate(
        conn,
        gate=config,
        redaction=RedactionConfig(),
        raw_store=RawStore(tmp_path / "raw"),
    )

    result = gate.ingest(raw_event("deploy finished"), now=NOW)

    assert result is not None
    assert result.matched_rules == ("deploy",)
    assert result.passes is True


def test_message_source_kind_and_weight(setup) -> None:
    conn, gate, _ = setup

    result = gate.ingest(
        raw_event("회의 일정 확인 부탁드립니다", source="message:stub", kind="message"),
        now=NOW,
    )

    assert result is not None
    assert rows(conn)[0]["kind"] == "message"
    assert rows(conn)[0]["source"] == "message:stub"

