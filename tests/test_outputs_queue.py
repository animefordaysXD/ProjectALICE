from __future__ import annotations

import json
import sqlite3
from datetime import datetime

import pytest

from alice.contracts.outputs import Suggestion
from alice.core.db import connect, init_db, utc_now
from alice.outputs.config import Notify
from alice.outputs.queue import SuggestionQueue, is_quiet


class FakeNotifier:
    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.delivered: list[tuple[str, str]] = []

    def notify(self, title: str, body: str) -> bool:
        if self.ok:
            self.delivered.append((title, body))
        return self.ok


@pytest.fixture
def conn(tmp_path):
    db = tmp_path / "alice.db"
    init_db(db)
    connection = connect(db, create=False)
    yield connection
    connection.close()


def test_quiet_hours_ranges() -> None:
    assert is_quiet(datetime(2026, 11, 22, 10, 0), "09:00-17:00") is True
    assert is_quiet(datetime(2026, 11, 22, 18, 0), "09:00-17:00") is False
    assert is_quiet(datetime(2026, 11, 22, 23, 0), "22:00-08:00") is True
    assert is_quiet(datetime(2026, 11, 22, 7, 0), "22:00-08:00") is True
    assert is_quiet(datetime(2026, 11, 22, 12, 0), "22:00-08:00") is False
    assert is_quiet(datetime(2026, 11, 22, 8, 0), "22:00-08:00") is False


def test_submit_and_deliver_respects_priority(conn) -> None:
    queue = SuggestionQueue(conn)
    queue.submit(Suggestion("low", 3, reason="r"))
    queue.submit(Suggestion("urgent", 1, reason="r"))
    queue.submit(Suggestion("useful", 2, reason="r"))
    notifier = FakeNotifier()
    config = Notify(
        quiet_hours="22:00-08:00",
        max_toasts_per_hour=2,
        min_toast_priority=2,
    )

    delivered = queue.deliver(
        notifier, config, now=datetime(2026, 11, 22, 12, 0)
    )

    assert delivered == 2
    assert [body for _, body in notifier.delivered] == ["urgent", "useful"]
    rows = conn.execute("SELECT text, status, shown_at FROM suggestions ORDER BY priority").fetchall()
    assert [(row["text"], row["status"], row["shown_at"] is not None) for row in rows] == [
        ("urgent", "shown", True),
        ("useful", "shown", True),
        ("low", "queued", False),
    ]

    # The hourly cap is now reached.
    assert queue.deliver(notifier, config, now=datetime(2026, 11, 22, 12, 10)) == 0


def test_quiet_hours_suppress_delivery(conn) -> None:
    queue = SuggestionQueue(conn)
    queue.submit(Suggestion("urgent", 1))
    notifier = FakeNotifier()

    delivered = queue.deliver(
        notifier,
        Notify(quiet_hours="22:00-08:00"),
        now=datetime(2026, 11, 22, 23, 0),
    )

    assert delivered == 0
    assert notifier.delivered == []


def test_digest_mode_does_not_toast(conn) -> None:
    queue = SuggestionQueue(conn)
    queue.submit(Suggestion("urgent", 1))
    notifier = FakeNotifier()

    assert queue.deliver(
        notifier,
        Notify(mode="digest"),
        now=datetime(2026, 11, 22, 12, 0),
    ) == 0


def test_notifier_failure_keeps_suggestion_queued(conn) -> None:
    queue = SuggestionQueue(conn)
    queue.submit(Suggestion("urgent", 1))

    assert queue.deliver(
        FakeNotifier(ok=False),
        Notify(quiet_hours="22:00-08:00"),
        now=datetime(2026, 11, 22, 12, 0),
    ) == 0
    assert conn.execute("SELECT status FROM suggestions").fetchone()[0] == "queued"


def test_source_event_ids_round_trip(conn) -> None:
    queue = SuggestionQueue(conn)
    queue.submit(Suggestion("check", 2, source_event_ids=(3, 5)))

    pending = queue.pending()

    assert pending[0].source_event_ids == (3, 5)


def test_cluster_submit_merges_into_canonical(conn) -> None:
    queue = SuggestionQueue(conn)
    first = queue.submit(
        Suggestion("Queue flush failed.", 2, cluster="queue-flush", source_event_ids=(1,))
    )
    second = queue.submit(
        Suggestion(
            "Queue flush failed again.",
            1,
            cluster="queue-flush",
            source_event_ids=(2,),
            steps=("Check disk",),
            evidence="214 errors",
        )
    )

    assert second == first
    rows = conn.execute("SELECT * FROM suggestions").fetchall()
    assert len(rows) == 1
    row = rows[0]
    assert row["priority"] == 1
    assert row["duplicate_count"] == 2
    assert json.loads(row["source_event_ids"]) == [1, 2]
    assert json.loads(row["steps"]) == ["Check disk"]
    assert row["evidence"] == "214 errors"
    assert row["cluster_key"] == "queue-flush"


def test_merge_existing_groups_by_cluster(conn) -> None:
    queue = SuggestionQueue(conn)
    conn.execute(
        "INSERT INTO suggestions (created, priority, text, status, cluster_key) "
        "VALUES (?, 2, 'first', 'queued', 'same-cluster')",
        (utc_now(),),
    )
    conn.execute(
        "INSERT INTO suggestions (created, priority, text, status, cluster_key) "
        "VALUES (?, 1, 'second', 'queued', 'same-cluster')",
        (utc_now(),),
    )
    conn.execute(
        "INSERT INTO suggestions (created, priority, text, status, cluster_key) "
        "VALUES (?, 2, 'other', 'queued', 'other-cluster')",
        (utc_now(),),
    )
    conn.commit()

    assert queue.merge_existing() == 1
    active = conn.execute(
        "SELECT * FROM suggestions WHERE status != 'merged' ORDER BY id"
    ).fetchall()
    merged = conn.execute(
        "SELECT * FROM suggestions WHERE status = 'merged'"
    ).fetchall()
    assert len(active) == 2
    assert active[0]["duplicate_count"] == 2
    assert active[0]["priority"] == 1
    assert len(merged) == 1
    assert merged[0]["merged_into"] == active[0]["id"]
