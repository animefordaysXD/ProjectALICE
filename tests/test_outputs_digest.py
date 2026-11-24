from __future__ import annotations

from datetime import datetime, timezone

import pytest

from alice.contracts.outputs import BudgetSnapshot, Suggestion
from alice.core.db import connect, init_db
from alice.outputs.config import Notify
from alice.outputs.digest import DigestWriter
from alice.outputs.queue import SuggestionQueue

NOON = datetime(2026, 11, 22, 12, 0)
STAMP = NOON.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class FakeNotifier:
    def __init__(self) -> None:
        self.delivered: list[tuple[str, str]] = []

    def notify(self, title: str, body: str) -> bool:
        self.delivered.append((title, body))
        return True


@pytest.fixture
def conn(tmp_path):
    db = tmp_path / "alice.db"
    init_db(db)
    connection = connect(db, create=False)
    yield connection
    connection.close()


def snapshot(alerts: tuple[str, ...] = ()) -> BudgetSnapshot:
    return BudgetSnapshot(
        paused=None,
        calls_today=4,
        spend_today=0.1234,
        soft_cap=1.0,
        hard_cap=2.5,
        alerts=alerts,
    )


def test_digest_lists_suggestions_and_alerts(conn, tmp_path) -> None:
    SuggestionQueue(conn).submit(
        Suggestion("check the disk", 1, (1, 2), "disk warning"),
        created=STAMP,
    )
    conn.execute(
        "INSERT INTO triage_batches (created, event_ids, decision, priority, reason, attempts) "
        "VALUES ('2026-11-22T01:00:00Z', '[1]', 'escalate', 1, 'ambiguous', 1)"
    )
    conn.execute(
        "INSERT INTO budget_alerts (ts, tier, reason) VALUES (?, 'triage', 'hard cap reached')",
        (STAMP,),
    )
    conn.commit()
    writer = DigestWriter(
        conn,
        tmp_path,
        Notify(mode="digest+toast"),
        lambda: snapshot(("hard cap reached",)),
    )
    notifier = FakeNotifier()

    result = writer.write(now=NOON, notifier=notifier)

    assert result.created is True
    assert result.notified is True
    text = result.path.read_text(encoding="utf-8")
    assert "check the disk" in text
    assert "disk warning" in text
    assert "hard cap reached" in text
    assert "$0.1234" in text
    assert "Paused: no" in text
    assert "Triage batches: 1" in text

    second = writer.write(now=datetime(2026, 11, 22, 12, 30), notifier=notifier)
    assert second.created is False
    assert second.notified is False
    assert len(notifier.delivered) == 1


def test_digest_respects_quiet_hours_for_the_summary_toast(conn, tmp_path) -> None:
    writer = DigestWriter(conn, tmp_path, Notify(mode="digest+toast"), snapshot)
    notifier = FakeNotifier()

    result = writer.write(now=datetime(2026, 11, 22, 23, 0), notifier=notifier)

    assert result.path.is_file()
    assert result.notified is False
    assert notifier.delivered == []


def test_digest_mode_has_no_toast(conn, tmp_path) -> None:
    writer = DigestWriter(conn, tmp_path, Notify(mode="digest"), snapshot)
    notifier = FakeNotifier()

    result = writer.write(now=datetime(2026, 11, 22, 12, 0), notifier=notifier)

    assert result.notified is False
    assert notifier.delivered == []
