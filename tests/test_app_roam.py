from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from alice.app.activity import Activity
from alice.app.roam import RoamService
from alice.app.roam_config import RoamConfig
from alice.contracts.models import Completion
from alice.contracts.tools import ToolResult, ToolSpec
from alice.core.db import connect, init_db
from alice.outputs.queue import SuggestionQueue


class StubTools:
    def __init__(self, allowed=None):
        self.calls = []
        self.allowed = allowed or {
            "time.now",
            "system.snapshot",
            "activity.snapshot",
            "file.list",
            "file.read",
            "web.fetch",
            "calendar.upcoming",
        }

    def specs(self):
        return tuple(
            ToolSpec(name, f"{name} description") for name in self.allowed
        )

    def execute(self, name, args=None):
        self.calls.append((name, dict(args or {})))
        return ToolResult(True, f"result of {name}", {})


class Router:
    def __init__(self, action=None, *, fail=False):
        self.action = action or {
            "action": "tool",
            "tool": "file.list",
            "args": {"path": "C:/Users", "limit": 5},
            "reason": "inspect",
            "note": "The root has a few folders.",
        }
        self.fail = fail
        self.calls = []

    def complete(self, tier, messages, max_tokens, *, purpose=None, cache_prefix_len=None):
        self.calls.append((purpose, messages))
        if self.fail:
            raise RuntimeError("provider offline")
        if purpose == "roam-summary":
            return Completion(
                text="# Roam summary\n\nAlice inspected the root and found folders.",
                input_tokens=50,
                output_tokens=20,
                cost_usd=0.0,
            )
        return Completion(
            text=json.dumps(self.action),
            input_tokens=50,
            output_tokens=20,
            cost_usd=0.0,
        )


class Growth:
    def __init__(self):
        self.observations = []
        self.rendered = 0

    def render(self):
        self.rendered += 1
        return "[GROWTH MEMORY]\n- (0.20, interest) Systems."

    def ingest_observation(self, **kwargs):
        self.observations.append(kwargs)
        return 1


@pytest.fixture
def conn(tmp_path: Path):
    db = tmp_path / "alice.db"
    init_db(db)
    connection = connect(db, create=False)
    yield connection
    connection.close()


def config(**kwargs):
    values = {
        "enabled": True,
        "idle_minutes": 5,
        "step_seconds": 1,
        "max_steps": 10,
        "memory_seconds": 0,
        "max_suggestions": 3,
        "allow_when_active": True,
    }
    values.update(kwargs)
    return RoamConfig(**values)


def activity(idle=0):
    return Activity(
        app="code.exe",
        title="notes.py",
        category="coding",
        idle_seconds=idle,
        captured_at=datetime.now().astimezone().isoformat(),
    )


def service(tmp_path, conn, router=None, tools=None, growth=None):
    return RoamService(
        conn,
        router or Router(),
        tools or StubTools(),
        config(),
        tmp_path,
        queue=SuggestionQueue(conn),
        growth=growth or Growth(),
        activity=lambda: activity(),
        screen=lambda: None,
        accessibility=lambda: None,
    )


def test_roam_runs_to_target_and_writes_summary(tmp_path, conn) -> None:
    tools = StubTools()
    router = Router()
    roam = service(tmp_path, conn, router, tools)
    session_id = roam.create_session(2, force=True)
    result = roam.run(session_id, force=True, sleep=lambda _seconds: None)

    assert result["status"] == "completed"
    assert result["steps"] == 2
    assert result["active_seconds"] >= 2
    summary = Path(result["summary_path"])
    assert summary.is_file()
    assert "Roam summary" in summary.read_text(encoding="utf-8")
    assert len(tools.calls) == 2
    assert (Path(result["session_dir"]) / "journal.md").is_file()


def test_roam_defers_model_stop_until_target(tmp_path, conn) -> None:
    router = Router({"action": "stop", "reason": "nothing useful"})
    roam = service(tmp_path, conn, router)
    session_id = roam.create_session(2, force=True)
    result = roam.run(session_id, force=True, sleep=lambda _seconds: None)
    assert result["status"] == "completed"
    assert result["steps"] == 2
    assert result["active_seconds"] >= 2
    assert result["stop_reason"] == "target reached"


def test_roam_falls_back_for_disallowed_tool(tmp_path, conn) -> None:
    router = Router(
        {
            "action": "tool",
            "tool": "file.write",
            "args": {"path": "C:/tmp/x", "text": "no"},
            "reason": "bad",
        }
    )
    tools = StubTools()
    roam = service(tmp_path, conn, router, tools)
    session_id = roam.create_session(1, force=True)
    roam.run(session_id, force=True, sleep=lambda _seconds: None)
    assert tools.calls[0][0] == "file.list"
    assert all(name != "file.write" for name, _args in tools.calls)


def test_roam_records_growth_observation(tmp_path, conn) -> None:
    growth = Growth()
    roam = service(tmp_path, conn, growth=growth)
    session_id = roam.create_session(1, force=True)
    roam.run(session_id, force=True, sleep=lambda _seconds: None)
    assert growth.observations
    assert growth.observations[0]["source"] == "roam"


def test_roam_status_and_stop(tmp_path, conn) -> None:
    roam = service(tmp_path, conn)
    session_id = roam.create_session(600, force=True)
    row = roam.session(session_id)
    assert row["status"] == "created"
    roam.stop(session_id, "test stop")
    row = roam.session(session_id)
    assert row["status"] == "stopped"
    assert row["stop_reason"] == "test stop"
