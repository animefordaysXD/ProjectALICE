from __future__ import annotations

import shutil
import sqlite3
import sys
from pathlib import Path

import pytest

from alice.core.actions import ActionError, ActionRunner
from alice.core.config import Actions
from alice.core.db import connect, init_db, utc_now


@pytest.fixture
def conn(tmp_path: Path):
    db = tmp_path / "alice.db"
    init_db(db)
    connection = connect(db, create=False)
    connection.execute(
        "INSERT INTO suggestions (created, priority, text, status) "
        "VALUES (?, 2, 'Check the install.', 'queued')",
        (utc_now(),),
    )
    connection.commit()
    yield connection
    connection.close()


def runner(conn: sqlite3.Connection, root: Path, *, enabled: bool = True):
    return ActionRunner(
        conn,
        Actions(
            enabled=enabled,
            allowed_programs=("cmd",),
            timeout_seconds=10,
            max_output_chars=2000,
        ),
        root,
    )


@pytest.mark.skipif(shutil.which("cmd") is None, reason="cmd not on PATH")
def test_propose_and_execute_cmd(conn, tmp_path: Path) -> None:
    action_id = runner(conn, tmp_path).propose(
        1,
        summary="Print a test line",
        program="cmd",
        args=["/c", "echo action-ok"],
        risk="low",
    )

    with pytest.raises(ActionError, match="RUN"):
        runner(conn, tmp_path).execute(action_id, confirmation="yes")

    result = runner(conn, tmp_path).execute(action_id, confirmation="RUN")

    assert result.status == "succeeded"
    assert result.exit_code == 0
    assert "action-ok" in (result.output or "")


def test_program_allowlist_and_path(conn, tmp_path: Path) -> None:
    with pytest.raises(ActionError, match="allowed_programs"):
        runner(conn, tmp_path).propose(
            1, summary="bad", program="powershell", args=["-Command", "dir"]
        )

    with pytest.raises(ActionError, match="bare executable"):
        runner(conn, tmp_path).propose(
            1, summary="bad path", program=str(Path(sys.executable)), args=[]
        )


def test_cwd_must_stay_under_root(conn, tmp_path: Path) -> None:
    outside = tmp_path.parent

    with pytest.raises(ActionError, match="under the app root"):
        runner(conn, tmp_path).propose(
            1,
            summary="bad cwd",
            program="cmd",
            args=[],
            cwd=str(outside),
        )


def test_disabled_actions_refuse_execution(conn, tmp_path: Path) -> None:
    action_id = runner(conn, tmp_path).propose(
        1, summary="no run", program="cmd", args=["/c", "echo 1"]
    )

    with pytest.raises(ActionError, match="disabled"):
        runner(conn, tmp_path, enabled=False).execute(
            action_id, confirmation="RUN"
        )
