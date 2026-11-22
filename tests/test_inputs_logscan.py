from __future__ import annotations

import os
import time
from pathlib import Path

from alice.core.db import connect, init_db
from alice.core.events import CursorStore
from alice.inputs.config import Source
from alice.inputs.sensors.logscan import (
    LogScanSensor,
    coerce_globs,
    discover_log_files,
)


def touch(path: Path, text: str = "line\n", *, age_days: int = 0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    if age_days:
        stamp = time.time() - age_days * 86400
        os.utime(path, (stamp, stamp))
    return path


def test_discovery_matches_extensions_and_skips_excluded(tmp_path: Path) -> None:
    keep_log = touch(tmp_path / "logs" / "a.log")
    keep_txt = touch(tmp_path / "logs" / "b.txt")
    keep_jsonl = touch(tmp_path / "logs" / "c.jsonl")
    touch(tmp_path / "logs" / "readme.md")
    touch(tmp_path / "logs" / "old.log", age_days=40)
    binary = tmp_path / "logs" / "cache.log"
    binary.write_bytes(b"\x00\x01\x02SQLite")
    touch(tmp_path / "skip" / "inside.log")
    touch(tmp_path / "excluded" / "inside.log")
    deep = touch(tmp_path / "nested" / "deeper" / "d.log")

    result = discover_log_files(
        tmp_path,
        globs=("*.log", "*.txt", "*.jsonl"),
        exclude_dirs={"skip"},
        exclude_paths=(tmp_path / "excluded",),
        max_files=100,
        recent_days=14,
    )

    found = set(result.files)
    assert found == {keep_log, keep_txt, keep_jsonl, deep}
    assert result.truncated is False
    assert result.errors == 0

    binary_ok = discover_log_files(
        tmp_path,
        globs=("*.log",),
        exclude_dirs={"skip"},
        exclude_paths=(tmp_path / "excluded",),
        max_files=100,
        recent_days=14,
        text_only=False,
    )
    assert binary in binary_ok.files


def test_discovery_respects_file_cap_and_depth(tmp_path: Path) -> None:
    for index in range(5):
        touch(tmp_path / "logs" / f"{index}.log")
    touch(tmp_path / "one" / "two" / "deep.log")

    capped = discover_log_files(tmp_path, globs=("*.log",), max_files=2, recent_days=0)
    assert len(capped.files) == 2
    assert capped.truncated is True

    shallow = discover_log_files(
        tmp_path, globs=("*.log",), max_files=100, max_depth=1, recent_days=0
    )
    assert all("two" not in path.parts for path in shallow.files)


def test_sensor_tails_files_and_commits_cursor(tmp_path: Path) -> None:
    log = touch(tmp_path / "app.log", "first line\n")
    db = tmp_path / "alice.db"
    init_db(db)
    conn = connect(db, create=False)
    try:
        source = Source(
            type="logscan",
            options={
                "path": str(tmp_path),
                "globs": ["*.log"],
                "from_start": True,
                "max_lines": 10,
            },
        )
        first = LogScanSensor(source, root=tmp_path, cursors=CursorStore(conn))
        events = first.poll()
        assert [event.text for event in events] == ["first line"]
        assert events[0].source == "logfile:app.log"
        first.commit()

        with log.open("a", encoding="utf-8") as handle:
            handle.write("second line\n")
        second = LogScanSensor(source, root=tmp_path, cursors=CursorStore(conn))
        assert [event.text for event in second.poll()] == ["second line"]
        second.commit()
        assert conn.execute("SELECT COUNT(*) FROM cursors").fetchone()[0] == 1
    finally:
        conn.close()


def test_sensor_refreshes_cached_file_list(tmp_path: Path, monkeypatch) -> None:
    clock = [1000.0]
    monkeypatch.setattr(
        "alice.inputs.sensors.logscan.time.monotonic", lambda: clock[0]
    )
    db = tmp_path / "alice.db"
    init_db(db)
    conn = connect(db, create=False)
    try:
        source = Source(
            type="logscan",
            options={
                "path": str(tmp_path),
                "globs": ["*.log"],
                "from_start": True,
                "refresh_minutes": 30,
            },
        )
        sensor = LogScanSensor(source, root=tmp_path, cursors=CursorStore(conn))
        assert sensor.poll() == []

        touch(tmp_path / "new.log", "later\n")
        assert sensor.poll() == []  # still the cached empty list

        clock[0] += 31 * 60
        assert [event.text for event in sensor.poll()] == ["later"]
    finally:
        conn.close()


def test_coerce_globs() -> None:
    assert coerce_globs("*.log, *.txt") == ("*.log", "*.txt")
    assert coerce_globs(["*.log"]) == ("*.log",)
    assert coerce_globs(None)[0] == "*.log"
