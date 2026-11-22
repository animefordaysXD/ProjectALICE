from __future__ import annotations

import json
import os
import sqlite3
from datetime import date, datetime, time, timedelta, timezone

import pytest

from alice.brain.config import Consolidation
from alice.brain.consolidate import (
    ConsolidationFormatError,
    ConsolidationService,
    parse_daily_summary,
    parse_hourly_summary,
)
from alice.contracts.models import Completion
from alice.core.config import Retention
from alice.core.db import connect, init_db
from alice.core.events import Ledger, RawStore

LOCAL = datetime.now().astimezone().tzinfo
TARGET = date(2026, 11, 21)
NOW = datetime(2026, 11, 22, 3, 30, tzinfo=LOCAL)


class StubRouter:
    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str | None, int | None]] = []

    def complete(self, tier, messages, max_tokens, *, purpose=None, cache_prefix_len=None):
        self.calls.append((purpose, cache_prefix_len))
        text = self.responses.pop(0) if self.responses else ""
        return Completion(text=text, input_tokens=10, output_tokens=5)


class StubMemory:
    def __init__(self) -> None:
        self.summaries: dict[tuple[str, str], str] = {}
        self.proposals: list[tuple[str, str, str]] = []

    def hard_block(self) -> str:
        return "Project: Alice"

    def summary_exists(self, level, start) -> bool:
        return (level, start) in self.summaries

    def summary_text(self, level, start):
        return self.summaries.get((level, start))

    def record_summary(self, level, start, end, text):
        self.summaries[(level, start)] = text
        return len(self.summaries)

    def propose(self, key, value, reason):
        self.proposals.append((key, value, reason))
        return len(self.proposals)


@pytest.fixture
def conn(tmp_path):
    db = tmp_path / "alice.db"
    init_db(db)
    connection = connect(db, create=False)
    yield connection
    connection.close()


def local_ts(hour: int, minute: int = 0, *, day: date = TARGET) -> str:
    return (
        datetime.combine(day, time(hour, minute), tzinfo=LOCAL)
        .astimezone(timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    )


def add_event(conn, text: str, ts: str, *, raw_ref=None, digest=None) -> int:
    cursor = conn.execute(
        """
        INSERT INTO events (ts, source, kind, content_hash, text_redacted, raw_ref)
        VALUES (?, 'logfile:app.log', 'log', ?, ?, ?)
        """,
        (ts, digest or f"h{abs(hash(text + ts))}", text, raw_ref),
    )
    conn.commit()
    return int(cursor.lastrowid)


def service(conn, responses, memory=None, raw_store=None, max_tokens=6000):
    return ConsolidationService(
        conn,
        router=StubRouter(responses),
        ledger=Ledger(conn),
        memory=memory or StubMemory(),
        hard_memory=(memory or StubMemory()).hard_block,
        raw_store=raw_store,
        config=Consolidation(hour=3, max_events=500, max_tokens=max_tokens),
        retention=Retention(raw_payload_days=14),
    )


HOURLY = json.dumps({"summary": "hourly summary"})
DAILY = json.dumps(
    {
        "summary": "daily summary",
        "proposals": [
            {"key": "project", "value": "Alice", "reason": "stable project"}
        ],
    }
)


def test_hourly_daily_and_proposals(conn) -> None:
    add_event(conn, "deploy failed", local_ts(9, 5))
    add_event(conn, "disk warning", local_ts(10, 15))
    memory = StubMemory()
    router_responses = [HOURLY, HOURLY, DAILY]
    svc = service(conn, router_responses, memory=memory)

    result = svc.run(TARGET, now=NOW)

    assert result.ran is True
    assert result.hours == 2
    assert result.proposals == 1
    assert memory.summaries[("hour", local_ts(9))] == "hourly summary"
    assert memory.summaries[("hour", local_ts(10))] == "hourly summary"
    assert memory.summaries[("day", local_ts(0))] == "daily summary"
    assert memory.proposals == [("project", "Alice", "stable project")]
    assert [purpose for purpose, _ in svc.router.calls] == [
        "consolidate-hour",
        "consolidate-hour",
        "consolidate-day",
    ]
    row = conn.execute("SELECT * FROM consolidation_runs").fetchone()
    assert row["day"] == "2026-11-21"
    assert row["summary_count"] == 2
    assert row["proposal_count"] == 1


def test_second_run_is_skipped(conn) -> None:
    add_event(conn, "event", local_ts(9))
    memory = StubMemory()
    svc = service(conn, [HOURLY, DAILY], memory=memory)
    svc.run(TARGET, now=NOW)
    calls = len(svc.router.calls)

    second = svc.run(TARGET, now=NOW)

    assert second.ran is False
    assert len(svc.router.calls) == calls


def test_no_events_means_no_model_call(conn) -> None:
    svc = service(conn, [])
    result = svc.run(TARGET, now=NOW)

    assert result.ran is True
    assert result.hours == 0
    assert svc.router.calls == []
    assert conn.execute("SELECT COUNT(*) FROM consolidation_runs").fetchone()[0] == 1


def test_hourly_failure_retries(conn) -> None:
    add_event(conn, "event", local_ts(9))
    memory = StubMemory()
    svc = service(conn, ["bad", HOURLY, DAILY], memory=memory)

    result = svc.run(TARGET, now=NOW)

    assert result.hours == 1
    assert len(svc.router.calls) == 3


def test_daily_failure_leaves_run_unmarked(conn) -> None:
    add_event(conn, "event", local_ts(9))
    memory = StubMemory()
    svc = service(conn, [HOURLY, "bad", "still bad"], memory=memory)

    result = svc.run(TARGET, now=NOW)

    assert result.error
    assert result.hours == 1
    assert conn.execute("SELECT COUNT(*) FROM consolidation_runs").fetchone()[0] == 0


def test_raw_pruning_keeps_ledger_rows(conn, tmp_path) -> None:
    raw_root = tmp_path / "raw"
    old_file = raw_root / "aa" / "old.txt"
    old_file.parent.mkdir(parents=True)
    old_file.write_text("secret raw", encoding="utf-8")
    old_time = (datetime.now().timestamp()) - 30 * 86400
    os.utime(old_file, (old_time, old_time))
    old_ts = (
        datetime.combine(TARGET, time(0, 0), tzinfo=LOCAL)
        - timedelta(days=30)
    ).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    event_id = add_event(conn, "old event", old_ts, raw_ref="aa/old.txt")
    svc = service(conn, [], raw_store=RawStore(raw_root))

    result = svc.run(TARGET, now=NOW)

    assert result.raw_deleted == 1
    assert not old_file.exists()
    row = conn.execute("SELECT raw_ref FROM events WHERE id = ?", (event_id,)).fetchone()
    assert row["raw_ref"] is None
    assert conn.execute("SELECT COUNT(*) FROM events WHERE id = ?", (event_id,)).fetchone()[0] == 1


def test_parse_validation() -> None:
    assert parse_hourly_summary('{"summary":"ok"}') == "ok"
    with pytest.raises(ConsolidationFormatError):
        parse_hourly_summary('{"summary":""}')

    summary, proposals = parse_daily_summary(DAILY)
    assert summary == "daily summary"
    assert proposals[0].key == "project"
    with pytest.raises(ConsolidationFormatError, match="at most"):
        parse_daily_summary(
            '{"summary":"x","proposals":[' + ",".join(["{}"] * 4) + "]}"
        )
