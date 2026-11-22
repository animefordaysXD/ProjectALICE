from __future__ import annotations

from pathlib import Path

import pytest

from alice.inputs.config import Source
from alice.core.db import connect, init_db
from alice.core.events import CursorStore
from alice.inputs.sensors.logfile import LogfileSensor


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
    glob: str = "*.log",
) -> tuple[LogfileSensor, Path]:
    logs = tmp_path / "logs"
    logs.mkdir(exist_ok=True)
    options: dict[str, object] = {"path": str(logs), "glob": glob, "from_start": from_start}
    if max_lines is not None:
        options["max_lines"] = max_lines
    sensor = LogfileSensor(
        Source(type="logfile", options=options), root=tmp_path, cursors=store
    )
    return sensor, logs


def texts(events) -> list[str]:
    return [event.text for event in events]


def test_first_poll_tails_from_end_by_default(tmp_path: Path, store: CursorStore) -> None:
    sensor, logs = make_sensor(tmp_path, store, from_start=False)
    log = logs / "app.log"
    log.write_text("old line\n", encoding="utf-8")

    assert sensor.poll() == []

    with log.open("a", encoding="utf-8") as handle:
        handle.write("new line\n")
    events = sensor.poll()
    sensor.commit()

    assert texts(events) == ["new line"]
    assert events[0].source == "logfile:app.log"
    assert events[0].kind == "log"


def test_from_start_reads_existing_content(tmp_path: Path, store: CursorStore) -> None:
    sensor, logs = make_sensor(tmp_path, store, from_start=True)
    (logs / "app.log").write_text("first\nsecond\n", encoding="utf-8")

    assert texts(sensor.poll()) == ["first", "second"]


def test_partial_last_line_waits_for_newline(tmp_path: Path, store: CursorStore) -> None:
    sensor, logs = make_sensor(tmp_path, store, from_start=True)
    log = logs / "app.log"
    log.write_text("complete\npartial", encoding="utf-8")

    assert texts(sensor.poll()) == ["complete"]

    log.write_text("complete\npartial done\n", encoding="utf-8")
    assert texts(sensor.poll()) == ["partial done"]


def test_restart_resumes_without_duplicates(tmp_path: Path, store: CursorStore) -> None:
    sensor, logs = make_sensor(tmp_path, store, from_start=True)
    (logs / "app.log").write_text("a\nb\nc\n", encoding="utf-8")
    assert len(sensor.poll()) == 3
    sensor.commit()

    restarted, _ = make_sensor(tmp_path, store, from_start=True)

    assert restarted.poll() == []


def test_rotation_reads_the_new_file(tmp_path: Path, store: CursorStore) -> None:
    sensor, logs = make_sensor(tmp_path, store, from_start=True)
    log = logs / "app.log"
    log.write_text("old\n", encoding="utf-8")
    assert texts(sensor.poll()) == ["old"]
    sensor.commit()

    log.rename(logs / "app.log.1")
    (logs / "app.log").write_text("new\n", encoding="utf-8")

    assert texts(sensor.poll()) == ["new"]


def test_truncation_resets_the_offset(tmp_path: Path, store: CursorStore) -> None:
    sensor, logs = make_sensor(tmp_path, store, from_start=True)
    log = logs / "app.log"
    log.write_text("first line\n", encoding="utf-8")
    assert texts(sensor.poll()) == ["first line"]
    sensor.commit()

    log.write_text("", encoding="utf-8")
    assert sensor.poll() == []

    log.write_text("after truncate\n", encoding="utf-8")
    assert texts(sensor.poll()) == ["after truncate"]


def test_invalid_bytes_do_not_crash(tmp_path: Path, store: CursorStore) -> None:
    sensor, logs = make_sensor(tmp_path, store, from_start=True)
    (logs / "app.log").write_bytes(b"bad \xff\xfe bytes\n")

    events = sensor.poll()

    assert len(events) == 1
    assert "bad " in events[0].text
    assert "\ufffd" in events[0].text


def test_korean_lines_are_read_as_utf8(tmp_path: Path, store: CursorStore) -> None:
    sensor, logs = make_sensor(tmp_path, store, from_start=True)
    (logs / "app.log").write_text("배포 실패: 로그를 확인하세요\n", encoding="utf-8")

    assert texts(sensor.poll()) == ["배포 실패: 로그를 확인하세요"]


def test_max_lines_per_poll(tmp_path: Path, store: CursorStore) -> None:
    sensor, logs = make_sensor(tmp_path, store, from_start=True, max_lines=2)
    (logs / "app.log").write_text("1\n2\n3\n4\n5\n", encoding="utf-8")

    assert texts(sensor.poll()) == ["1", "2"]
    sensor.commit()
    assert texts(sensor.poll()) == ["3", "4"]
    sensor.commit()
    assert texts(sensor.poll()) == ["5"]


def test_rollback_makes_the_next_poll_repeat(tmp_path: Path, store: CursorStore) -> None:
    sensor, logs = make_sensor(tmp_path, store, from_start=True)
    (logs / "app.log").write_text("a\nb\n", encoding="utf-8")

    assert texts(sensor.poll()) == ["a", "b"]
    sensor.rollback()

    assert texts(sensor.poll()) == ["a", "b"]


def test_glob_matches_multiple_files(tmp_path: Path, store: CursorStore) -> None:
    sensor, logs = make_sensor(tmp_path, store, from_start=True)
    (logs / "a.log").write_text("from a\n", encoding="utf-8")
    (logs / "b.log").write_text("from b\n", encoding="utf-8")
    (logs / "ignored.txt").write_text("ignored\n", encoding="utf-8")

    events = sensor.poll()

    assert sorted(texts(events)) == ["from a", "from b"]
    assert {event.source for event in events} == {"logfile:a.log", "logfile:b.log"}


def test_rotated_fixture_pair_is_read(tmp_path: Path, store: CursorStore) -> None:
    fixtures = Path(__file__).parent / "fixtures" / "logs" / "rotated"
    logs = tmp_path / "logs"
    logs.mkdir()
    for name in ("app.log", "app.log.1"):
        (logs / name).write_bytes((fixtures / name).read_bytes())

    sensor = LogfileSensor(
        Source(
            type="logfile",
            options={
                "path": str(logs),
                "glob": "app.log*",
                "from_start": True,
                "encoding": "utf-8",
            },
        ),
        root=tmp_path,
        cursors=store,
    )

    events = sensor.poll()

    assert len(events) == 5
    assert {event.source for event in events} == {"logfile:app.log", "logfile:app.log.1"}


def test_blank_lines_are_skipped(tmp_path: Path, store: CursorStore) -> None:
    sensor, logs = make_sensor(tmp_path, store, from_start=True)
    (logs / "app.log").write_text("\nvalue\n\n", encoding="utf-8")

    assert texts(sensor.poll()) == ["value"]


def test_missing_directory_is_not_an_error(tmp_path: Path, store: CursorStore) -> None:
    sensor = LogfileSensor(
        Source(type="logfile", options={"path": str(tmp_path / "nope"), "from_start": True}),
        root=tmp_path,
        cursors=store,
    )

    assert sensor.poll() == []


def test_utf8_bom_is_stripped(tmp_path: Path, store: CursorStore) -> None:
    sensor, logs = make_sensor(tmp_path, store, from_start=True)
    (logs / "app.log").write_bytes(b"\xef\xbb\xbf" + "배포 실패\n".encode("utf-8"))

    assert texts(sensor.poll()) == ["배포 실패"]


def test_line_longer_than_poll_budget_still_advances(
    tmp_path: Path, store: CursorStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    import alice.inputs.sensors.logfile as logfile_module

    monkeypatch.setattr(logfile_module, "MAX_BYTES_PER_POLL", 16)
    sensor, logs = make_sensor(tmp_path, store, from_start=True)
    (logs / "app.log").write_bytes(b"x" * 40)

    first = sensor.poll()
    second = sensor.poll()
    third = sensor.poll()

    assert len(first) == len(second) == 1
    assert first[0].text == "x" * 16
    # The last 8 bytes are an incomplete line: wait for its newline.
    assert third == []

    with (logs / "app.log").open("ab") as handle:
        handle.write(b"\n")

    assert texts(sensor.poll()) == ["xxxxxxxx"]
    assert sensor.poll() == []

