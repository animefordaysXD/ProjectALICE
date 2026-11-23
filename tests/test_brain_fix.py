from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from alice.brain.fix import FixFormatError, FixPlanner, parse_fix_plan
from alice.contracts.models import Completion
from alice.core.db import connect, init_db, utc_now


class StubRouter:
    def __init__(self, text: str) -> None:
        self.text = text
        self.calls = []

    def complete(self, tier, messages, max_tokens, *, purpose=None, cache_prefix_len=None):
        self.calls.append((tier, purpose, tuple(messages)))
        return Completion(text=self.text, input_tokens=10, output_tokens=5)


@pytest.fixture
def conn(tmp_path: Path):
    db = tmp_path / "alice.db"
    init_db(db)
    connection = connect(db, create=False)
    connection.execute(
        "INSERT INTO suggestions (created, priority, text, reason, status) "
        "VALUES (?, 2, 'Python install failed.', 'installer exit 1618', 'queued')",
        (utc_now(),),
    )
    connection.commit()
    yield connection
    connection.close()


def test_parse_fix_plan() -> None:
    text = json.dumps(
        {
            "summary": "Retry the installer",
            "program": "winget",
            "args": ["upgrade", "--id", "Python.Python.3.12"],
            "cwd": None,
            "risk": "medium",
            "manual_steps": ["Close other installers first"],
        }
    )

    plan = parse_fix_plan(text, ("winget",))

    assert plan.program == "winget"
    assert plan.args == ("upgrade", "--id", "Python.Python.3.12")
    assert plan.manual_steps == ("Close other installers first",)


def test_parse_fix_plan_rejects_disallowed_program() -> None:
    text = json.dumps(
        {
            "summary": "bad",
            "program": "cmd",
            "args": ["/c", "dir"],
            "risk": "high",
            "manual_steps": [],
        }
    )

    with pytest.raises(FixFormatError, match="allowed_programs"):
        parse_fix_plan(text, ("winget",))


def test_planner_uses_suggestion_and_allows_manual_only(conn) -> None:
    router = StubRouter(
        json.dumps(
            {
                "summary": "Reinstall manually",
                "program": None,
                "args": [],
                "cwd": None,
                "risk": "medium",
                "manual_steps": ["Close pending installs", "Run the python.org installer"],
            }
        )
    )

    plan = FixPlanner(
        conn, router=router, allowed_programs=("winget",)
    ).plan(1)

    assert plan.program is None
    assert plan.manual_steps
    _tier, purpose, messages = router.calls[0]
    assert purpose == "fix-plan"
    joined = "\n".join(message.content for message in messages)
    assert "Python install failed." in joined
    assert "winget" in joined
