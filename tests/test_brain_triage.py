from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from alice.brain.config import Triage
from alice.brain.triage import (
    TriageFormatError,
    TriageService,
    parse_decision,
)
from alice.contracts.models import Completion
from alice.core.db import connect, init_db, utc_now
from alice.core.events import Ledger

VALID = {
    "decision": "log",
    "priority": 3,
    "reason": "routine event",
    "suggestion_text": None,
    "recall_query": None,
}


class StubRouter:
    def __init__(self, responses: list[str] | None = None) -> None:
        self.responses = list(responses or [])
        self.calls: list[dict] = []

    def complete(
        self,
        tier: str,
        messages,
        max_tokens: int,
        *,
        purpose=None,
        cache_prefix_len=None,
    ) -> Completion:
        self.calls.append(
            {
                "tier": tier,
                "messages": messages,
                "max_tokens": max_tokens,
                "purpose": purpose,
                "cache_prefix_len": cache_prefix_len,
            }
        )
        text = self.responses.pop(0) if self.responses else json.dumps(VALID)
        return Completion(
            text=text, input_tokens=10, output_tokens=5, cost_usd=0.001
        )


@pytest.fixture
def conn(tmp_path: Path):
    db = tmp_path / "alice.db"
    init_db(db)
    connection = connect(db, create=False)
    yield connection
    connection.close()


def add_event(
    conn: sqlite3.Connection,
    text: str,
    *,
    salience: float = 0.8,
    digest: str | None = None,
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO events (ts, source, kind, content_hash, text_redacted, salience)
        VALUES (?, 'logfile:app.log', 'log', ?, ?, ?)
        """,
        (utc_now(), digest or f"h{abs(hash(text))}", text, salience),
    )
    conn.commit()
    return int(cursor.lastrowid)


def service(
    conn: sqlite3.Connection, router, *, max_tokens: int = 3000, sink=None
) -> TriageService:
    return TriageService(
        conn,
        router=router,
        ledger=Ledger(conn),
        hard_memory=lambda: "Project: Alice",
        config=Triage(interval_minutes=5, max_batch_tokens=max_tokens),
        min_salience=0.5,
        sink=sink,
    )


class RecordingSink:
    def __init__(self) -> None:
        self.items = []

    def submit(self, suggestion) -> int:
        self.items.append(suggestion)
        return len(self.items)


def status_of(conn: sqlite3.Connection, event_id: int) -> str:
    return conn.execute("SELECT status FROM events WHERE id = ?", (event_id,)).fetchone()[0]


def test_suggest_decision_is_queued(conn) -> None:
    router = StubRouter(
        [
            json.dumps(
                {
                    "decision": "suggest",
                    "priority": 2,
                    "reason": "disk pressure",
                    "suggestion_text": "Check free disk space.",
                    "recall_query": None,
                }
            )
        ]
    )
    sink = RecordingSink()
    add_event(conn, "disk warning")

    result = service(conn, router, sink=sink).run_once()

    assert result.decision and result.decision.decision == "suggest"
    assert len(sink.items) == 1
    assert sink.items[0].text == "Check free disk space."
    assert sink.items[0].priority == 2
    assert sink.items[0].source_event_ids == (1,)


def test_no_events_means_no_model_call(conn) -> None:
    router = StubRouter()

    result = service(conn, router).run_once()

    assert result.events == 0 and result.decision is None
    assert router.calls == []


def test_low_salience_events_are_not_sent(conn) -> None:
    add_event(conn, "noise", salience=0.1)
    router = StubRouter()

    result = service(conn, router).run_once()

    assert result.events == 0
    assert router.calls == []


def test_valid_log_decision_marks_events_and_records_batch(conn) -> None:
    first = add_event(conn, "disk full")
    second = add_event(conn, "disk still full")
    router = StubRouter([json.dumps(VALID)])

    result = service(conn, router).run_once()

    assert result.decision is not None and result.decision.decision == "log"
    assert status_of(conn, first) == status_of(conn, second) == "triaged"
    assert len(router.calls) == 1
    assert router.calls[0]["tier"] == "triage"
    assert router.calls[0]["purpose"] == "triage"
    assert router.calls[0]["cache_prefix_len"] and router.calls[0]["cache_prefix_len"] > 0
    batch = conn.execute("SELECT * FROM triage_batches").fetchone()
    assert batch["decision"] == "log"
    assert json.loads(batch["event_ids"]) == [first, second]
    assert batch["attempts"] == 1


def test_escalate_sets_escalated_status(conn) -> None:
    event_id = add_event(conn, "data loss possible")
    payload = dict(VALID, decision="escalate", priority=1, reason="possible data loss")
    service(conn, StubRouter([json.dumps(payload)])).run_once()

    assert status_of(conn, event_id) == "escalated"


def test_invalid_then_valid_retries_once(conn) -> None:
    event_id = add_event(conn, "payment failed")
    router = StubRouter(["not json", json.dumps(dict(VALID, decision="suggest"))])

    result = service(conn, router).run_once()

    assert len(router.calls) == 2
    assert result.attempts == 2
    assert status_of(conn, event_id) == "triaged"
    assert result.decision.decision == "suggest"


def test_two_invalid_replies_leave_events_new(conn) -> None:
    event_id = add_event(conn, "ambiguous")
    router = StubRouter(["nope", "still nope"])

    result = service(conn, router).run_once()

    assert len(router.calls) == 2
    assert result.decision is None
    assert result.error
    assert status_of(conn, event_id) == "new"
    assert conn.execute("SELECT COUNT(*) FROM triage_batches").fetchone()[0] == 0


def test_suggestion_and_recall_are_recorded(conn) -> None:
    add_event(conn, "deploy failed")
    payload = dict(
        VALID,
        decision="suggest",
        priority=2,
        reason="deploy failure needs attention",
        suggestion_text="Check the deploy logs.",
        recall_query="deploy failure",
    )

    service(conn, StubRouter([json.dumps(payload)])).run_once()

    batch = conn.execute("SELECT * FROM triage_batches").fetchone()
    assert batch["suggestion_text"] == "Check the deploy logs."
    assert batch["recall_query"] == "deploy failure"


def test_prefix_is_stable_across_runs(conn) -> None:
    add_event(conn, "first batch")
    router = StubRouter()
    triage = service(conn, router)
    triage.run_once()
    add_event(conn, "second batch")
    triage.run_once()

    first, second = router.calls[0], router.calls[1]
    assert first["messages"][0].content == second["messages"][0].content
    assert first["messages"][1].content == second["messages"][1].content
    assert first["cache_prefix_len"] == second["cache_prefix_len"]


def test_batch_is_capped_by_tokens(conn) -> None:
    for index in range(20):
        add_event(conn, f"event {index} " + "x" * 100)
    router = StubRouter()

    service(conn, router, max_tokens=80).run_once()

    assert len(router.calls) == 1
    batch = conn.execute("SELECT event_ids FROM triage_batches").fetchone()
    assert 1 <= len(json.loads(batch["event_ids"])) < 20


def test_200_event_replay(conn) -> None:
    for index in range(200):
        add_event(conn, f"event {index}", digest=f"replay-{index}")
    router = StubRouter()
    triage = service(conn, router, max_tokens=120)

    runs = 0
    while True:
        result = triage.run_once()
        runs += 1
        if result.events == 0:
            break
        assert runs < 300

    assert runs > 2
    assert conn.execute("SELECT COUNT(*) FROM events WHERE status = 'triaged'").fetchone()[0] == 200
    assert conn.execute("SELECT COUNT(*) FROM events WHERE status = 'new'").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM triage_batches").fetchone()[0] == len(router.calls)


@pytest.mark.parametrize(
    "text",
    [
        "```json\n{\"decision\":\"log\",\"priority\":3,\"reason\":\"ok\"}\n```",
        "prefix {\"decision\":\"log\",\"priority\":\"2\",\"reason\":\"ok\"} suffix",
    ],
)
def test_parse_accepts_fenced_or_wrapped_json(text: str) -> None:
    decision = parse_decision(text)

    assert decision.decision == "log"
    assert decision.priority in (2, 3)


@pytest.mark.parametrize(
    "payload",
    [
        {"decision": "nope", "priority": 1, "reason": "x"},
        {"decision": "log", "priority": 4, "reason": "x"},
        {"decision": "log", "priority": 1, "reason": ""},
        {"decision": "log", "priority": 1, "reason": " ".join(["word"] * 21)},
        {"decision": "log", "priority": 1, "reason": "x", "suggestion_text": 5},
    ],
)
def test_parse_rejects_invalid_payloads(payload: dict) -> None:
    with pytest.raises(TriageFormatError):
        parse_decision(json.dumps(payload))
