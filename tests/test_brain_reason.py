from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest

from alice.brain.reason import (
    Escalation,
    ReasoningFormatError,
    ReasoningService,
    parse_reasoning,
)
from alice.contracts.memory import Recall, Task
from alice.contracts.outputs import Suggestion
from alice.core.db import connect, init_db
from alice.core.events import Ledger

NOW = datetime(2026, 11, 22, 12, 0, tzinfo=timezone.utc)

DRY_REASON = {
    "assessment": "Disk and deployment failures coincide.",
    "suggestions": [
        {"text": "Check disk usage.", "priority": 2, "reason": "likely cause"},
    ],
    "needs_heavy": False,
}


class StubRouter:
    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.calls: list[str] = []

    def complete(self, tier, messages, max_tokens, *, purpose=None, cache_prefix_len=None):
        from alice.contracts.models import Completion

        self.calls.append(tier)
        text = self.responses.pop(0) if self.responses else ""
        return Completion(text=text, input_tokens=10, output_tokens=5)


class StubMemory:
    def __init__(self) -> None:
        self.queries: list[str] = []

    def hard_block(self) -> str:
        return "Project: Alice"

    def recall(self, query, k=5, since=None):
        self.queries.append(query)
        return Recall(items=())

    def record_summary(self, *args) -> int:
        return 1

    def propose(self, *args) -> int:
        return 1


class StubTasks:
    def __init__(self) -> None:
        self.created: list[tuple[str, dict]] = []
        self.completed: list[tuple[int, str]] = []

    def create(self, goal, state=None):
        self.created.append((goal, dict(state or {})))
        return Task(
            id=len(self.created),
            goal=goal,
            state=dict(state or {}),
            status="open",
            created="2026-11-22T11:00:00Z",
            expires="2026-11-24T11:00:00Z",
        )

    def update_state(self, task_id, update):
        return self.create("updated", update)

    def complete(self, task_id, outcome):
        self.completed.append((task_id, outcome))
        return None


class StubSink:
    def __init__(self) -> None:
        self.items: list[Suggestion] = []

    def submit(self, suggestion):
        self.items.append(suggestion)
        return len(self.items)


@pytest.fixture
def conn(tmp_path):
    db = tmp_path / "alice.db"
    init_db(db)
    connection = connect(db, create=False)
    yield connection
    connection.close()


def add_escalation(conn: sqlite3.Connection, text: str = "deploy failed", *, reasoned_at=None) -> int:
    cursor = conn.execute(
        """
        INSERT INTO events
          (ts, source, kind, content_hash, text_redacted, salience, status)
        VALUES ('2026-11-22T10:00:00Z', 'logfile:app.log', 'log', ?, ?, 0.9, 'escalated')
        """,
        (f"h{abs(hash(text))}", text),
    )
    event_id = int(cursor.lastrowid)
    import json

    conn.execute(
        """
        INSERT INTO triage_batches
          (created, event_ids, decision, priority, reason, suggestion_text,
           recall_query, attempts, reasoned_at)
        VALUES ('2026-11-22T10:00:00Z', ?, 'escalate', 1, 'ambiguous', 'hint', 'deploy', 1, ?)
        """,
        (json.dumps([event_id]), reasoned_at),
    )
    conn.commit()
    return event_id


def make_service(conn, responses, *, memory=None, tasks=None, sink=None, heavy_calls=0, max_per_hour=6, max_heavy=1):
    memory = memory or StubMemory()
    tasks = tasks or StubTasks()
    sink = sink or StubSink()
    service = ReasoningService(
        conn,
        router=StubRouter(responses),
        ledger=Ledger(conn),
        memory=memory,
        tasks=tasks,
        sink=sink,
        hard_memory=memory.hard_block,
        max_escalations_per_hour=max_per_hour,
        max_heavy_per_day=max_heavy,
        heavy_calls_today=lambda: heavy_calls,
    )
    return service, memory, tasks, sink


def test_no_escalation_means_no_model_call(conn) -> None:
    service, _, _, _ = make_service(conn, [])

    result = service.run_once(now=NOW)

    assert result.ran is False
    assert service.router.calls == []


def test_reasoning_queues_suggestions_and_marks_done(conn) -> None:
    import json

    event_id = add_escalation(conn)
    service, memory, tasks, sink = make_service(conn, [json.dumps(DRY_REASON)])

    result = service.run_once(now=NOW)

    assert result.ran and result.suggestions == 1
    assert service.router.calls == ["reason"]
    assert memory.queries == ["deploy"]
    assert sink.items[0].text == "Check disk usage."
    assert sink.items[0].source_event_ids == (event_id,)
    assert conn.execute("SELECT status FROM events WHERE id = ?", (event_id,)).fetchone()[0] == "done"
    batch = conn.execute("SELECT * FROM triage_batches").fetchone()
    assert batch["reasoned_at"] is not None
    assert tasks.completed and "suggestions queued" in tasks.completed[0][1]


def test_invalid_reasoning_retries(conn) -> None:
    add_escalation(conn)
    import json

    service, _, _, sink = make_service(
        conn, ["not json", json.dumps(DRY_REASON)]
    )

    result = service.run_once(now=NOW)

    assert result.attempts == 2
    assert len(service.router.calls) == 2
    assert len(sink.items) == 1


def test_invalid_twice_leaves_escalation_pending(conn) -> None:
    event_id = add_escalation(conn)
    service, _, tasks, sink = make_service(conn, ["nope", "still nope"])

    result = service.run_once(now=NOW)

    assert result.error
    assert service.router.calls == ["reason", "reason"]
    assert sink.items == []
    assert conn.execute("SELECT status FROM events WHERE id = ?", (event_id,)).fetchone()[0] == "escalated"
    batch = conn.execute("SELECT * FROM triage_batches").fetchone()
    assert batch["reasoned_at"] is None
    assert batch["reason_attempts"] == 1
    assert tasks.completed


def test_hourly_limit_blocks_reasoning(conn) -> None:
    add_escalation(conn, reasoned_at="2026-11-22T11:30:00Z")
    add_escalation(conn, "second")
    service, _, _, _ = make_service(conn, [], max_per_hour=1)

    result = service.run_once(now=NOW)

    assert result.ran is False
    assert service.router.calls == []


def test_heavy_refinement_when_requested(conn) -> None:
    add_escalation(conn)
    import json

    reason = dict(DRY_REASON, needs_heavy=True)
    heavy = {
        "assessment": "Heavy review.",
        "suggestions": [
            {"text": "Run a backup check.", "priority": 1, "reason": "data risk"},
        ],
        "needs_heavy": False,
    }
    service, _, _, sink = make_service(
        conn, [json.dumps(reason), json.dumps(heavy)], max_heavy=1
    )

    result = service.run_once(now=NOW)

    assert result.used_heavy is True
    assert service.router.calls == ["reason", "heavy"]
    assert sink.items[0].priority == 1


def test_heavy_limit_keeps_reason_result(conn) -> None:
    add_escalation(conn)
    import json

    reason = dict(DRY_REASON, needs_heavy=True)
    service, _, _, sink = make_service(
        conn, [json.dumps(reason)], heavy_calls=1, max_heavy=1
    )

    result = service.run_once(now=NOW)

    assert result.used_heavy is False
    assert service.router.calls == ["reason"]
    assert len(sink.items) == 1


def test_parse_reasoning_validation() -> None:
    decision = parse_reasoning(
        '```json\n{"assessment":"ok","suggestions":[],"needs_heavy":false}\n```'
    )
    assert decision.assessment == "ok"

    with pytest.raises(ReasoningFormatError, match="priority"):
        parse_reasoning(
            '{"assessment":"x","suggestions":[{"text":"y","priority":9}]}'
        )
    with pytest.raises(ReasoningFormatError, match="30 words"):
        parse_reasoning(
            '{"assessment":"x","suggestions":[{"text":"y","priority":1,"reason":"'
            + " ".join(["w"] * 31)
            + '"}]}'
        )

    rich = parse_reasoning(
        '{"assessment":"x","suggestions":[{'
        '"text":"y","priority":2,"reason":"r",'
        '"steps":["check","verify"],"evidence":"log line",'
        '"category":"performance","confidence":0.75}]}'
    ).suggestions[0]
    assert rich.steps == ("check", "verify")
    assert rich.evidence == "log line"
    assert rich.category == "performance"
    assert rich.confidence == 0.75
    with pytest.raises(ReasoningFormatError, match="at most"):
        parse_reasoning(
            '{"assessment":"x","suggestions":['
            + ",".join(['{"text":"y","priority":1}'] * 4)
            + "]}"
        )
