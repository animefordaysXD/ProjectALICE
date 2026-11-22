from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from alice.core.db import connect, init_db
from alice.memory.errors import ProposalError
from alice.memory.fast import FastMemory

NOW = datetime(2026, 11, 21, 12, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self, moment: datetime) -> None:
        self.moment = moment

    def __call__(self) -> datetime:
        return self.moment


@pytest.fixture
def conn(tmp_path: Path):
    db = tmp_path / "alice.db"
    init_db(db)
    connection = connect(db, create=False)
    yield connection
    connection.close()


def test_create_and_update_state(conn) -> None:
    clock = Clock(NOW)
    fast = FastMemory(conn, ttl_hours=2, clock=clock)

    task = fast.create("investigate disk", {"step": 1})

    assert task.expires == "2026-11-21T14:00:00Z"
    updated = fast.update_state(task.id, {"step": 2, "note": "checked"})
    assert updated.state == {"step": 2, "note": "checked"}
    assert fast.open_count() == 1

    clock.moment = NOW + timedelta(hours=1)
    assert fast.expire_due() == []
    assert fast.get(task.id) is not None


def test_completion_archives_an_outcome(conn) -> None:
    fast = FastMemory(conn, clock=Clock(NOW))
    task = fast.create("triage batch")

    done = fast.complete(task.id, "no action needed")

    assert done is not None
    assert fast.get(task.id) is None
    row = conn.execute(
        "SELECT kind, source, text_redacted FROM events ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row["kind"] == "task_outcome"
    assert row["source"] == "memory:task"
    assert "no action needed" in row["text_redacted"]


def test_ttl_expiry_archives_and_deletes(conn) -> None:
    clock = Clock(NOW)
    fast = FastMemory(conn, ttl_hours=2, clock=clock)
    task = fast.create("stale task")

    clock.moment = NOW + timedelta(hours=3)
    expired = fast.expire_due()

    assert expired == [task.id]
    assert fast.get(task.id) is None
    row = conn.execute(
        "SELECT text_redacted FROM events WHERE kind = 'task_outcome'"
    ).fetchone()
    assert "expired" in row["text_redacted"]


def test_sanitizer_is_applied_to_outcomes(conn) -> None:
    fast = FastMemory(conn, clock=Clock(NOW), sanitize=lambda text: text.upper())
    task = fast.create("task")

    fast.complete(task.id, "secret detail")

    row = conn.execute("SELECT text_redacted FROM events").fetchone()
    assert "SECRET DETAIL" in row["text_redacted"]


def test_missing_task_errors(conn) -> None:
    fast = FastMemory(conn)

    with pytest.raises(ProposalError, match="not found"):
        fast.update_state(999, {"x": 1})
    assert fast.complete(999, "nothing") is None
