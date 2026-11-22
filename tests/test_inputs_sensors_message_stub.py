from __future__ import annotations

from pathlib import Path

import pytest

from alice.inputs.config import Source
from alice.core.db import connect, init_db
from alice.core.events import CursorStore
from alice.inputs.sensors.message_stub import StubMessageSensor


@pytest.fixture
def store(tmp_path: Path):
    db_path = tmp_path / "alice.db"
    init_db(db_path)
    conn = connect(db_path, create=False)
    yield CursorStore(conn)
    conn.close()


def make_sensor(
    tmp_path: Path,
    store: CursorStore,
    *,
    from_start: bool = True,
    max_lines: int | None = None,
) -> tuple[StubMessageSensor, Path]:
    path = tmp_path / "messages.txt"
    options: dict[str, object] = {
        "path": str(path),
        "adapter": "stub",
        "from_start": from_start,
    }
    if max_lines is not None:
        options["max_lines"] = max_lines
    sensor = StubMessageSensor(
        Source(type="message", options=options), root=tmp_path, cursors=store
    )
    return sensor, path


def test_reads_messages_from_start(tmp_path: Path, store: CursorStore) -> None:
    sensor, path = make_sensor(tmp_path, store)
    path.write_text("첫 번째 메시지\nsecond message\n", encoding="utf-8")

    events = sensor.poll()

    assert [event.text for event in events] == ["첫 번째 메시지", "second message"]
    assert {event.kind for event in events} == {"message"}
    assert {event.source for event in events} == {"message:stub"}


def test_only_new_lines_are_returned(tmp_path: Path, store: CursorStore) -> None:
    sensor, path = make_sensor(tmp_path, store)
    path.write_text("one\n", encoding="utf-8")
    assert len(sensor.poll()) == 1
    sensor.commit()

    with path.open("a", encoding="utf-8") as handle:
        handle.write("two\n")

    assert [event.text for event in sensor.poll()] == ["two"]


def test_restart_produces_no_duplicates(tmp_path: Path, store: CursorStore) -> None:
    sensor, path = make_sensor(tmp_path, store)
    path.write_text("one\ntwo\n", encoding="utf-8")
    sensor.poll()
    sensor.commit()

    restarted, _ = make_sensor(tmp_path, store)

    assert restarted.poll() == []


def test_blank_lines_are_skipped_but_cursor_advances(tmp_path: Path, store: CursorStore) -> None:
    sensor, path = make_sensor(tmp_path, store)
    path.write_text("a\n\nb\n", encoding="utf-8")

    assert [event.text for event in sensor.poll()] == ["a", "b"]
    sensor.commit()

    restarted, _ = make_sensor(tmp_path, store)
    assert restarted.poll() == []


def test_from_start_false_skips_existing(tmp_path: Path, store: CursorStore) -> None:
    sensor, path = make_sensor(tmp_path, store, from_start=False)
    path.write_text("old\n", encoding="utf-8")

    assert sensor.poll() == []

    with path.open("a", encoding="utf-8") as handle:
        handle.write("new\n")

    assert [event.text for event in sensor.poll()] == ["new"]


def test_max_lines_per_poll(tmp_path: Path, store: CursorStore) -> None:
    sensor, path = make_sensor(tmp_path, store, max_lines=1)
    path.write_text("a\nb\nc\n", encoding="utf-8")

    assert [event.text for event in sensor.poll()] == ["a"]
    sensor.commit()
    assert [event.text for event in sensor.poll()] == ["b"]
    sensor.commit()
    assert [event.text for event in sensor.poll()] == ["c"]


def test_missing_file_is_not_an_error(tmp_path: Path, store: CursorStore) -> None:
    sensor, _ = make_sensor(tmp_path, store)

    assert sensor.poll() == []

