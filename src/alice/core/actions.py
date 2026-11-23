"""Explicit, user-approved local fix actions.

A suggestion can have a proposed action, but nothing runs automatically. The
GUI shows the exact program, arguments, working directory, and risk, then asks
for a typed confirmation. The runner uses `shell=False`, resolves the program
from PATH, enforces an executable allowlist, and records every result locally.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .config import Actions
from .db import utc_now

logger = logging.getLogger(__name__)

CONFIRMATION = "RUN"


class ActionError(RuntimeError):
    """An action was rejected, invalid, or could not be executed."""


@dataclass(frozen=True)
class ActionRow:
    id: int
    suggestion_id: int
    created: str
    summary: str
    program: str
    args: tuple[str, ...]
    cwd: str | None
    risk: str | None
    status: str
    exit_code: int | None
    output: str | None
    error: str | None


class ActionRunner:
    def __init__(
        self, conn: sqlite3.Connection, config: Actions, root: Path | str
    ) -> None:
        self.conn = conn
        self.config = config
        self.root = Path(root).expanduser().resolve()

    def propose(
        self,
        suggestion_id: int,
        *,
        summary: str,
        program: str,
        args: list[str],
        cwd: str | None = None,
        risk: str | None = None,
    ) -> int:
        if not summary.strip():
            raise ActionError("action summary must not be empty")
        self._validate_program(program)
        clean_args = self._validate_args(args)
        workdir = self._validate_cwd(cwd)
        if self.conn.execute(
            "SELECT 1 FROM suggestions WHERE id = ?", (suggestion_id,)
        ).fetchone() is None:
            raise ActionError(f"suggestion {suggestion_id} does not exist")
        cursor = self.conn.execute(
            """
            INSERT INTO actions
              (suggestion_id, created, summary, program, args, cwd, risk, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'proposed')
            """,
            (
                suggestion_id,
                utc_now(),
                summary.strip(),
                program,
                json.dumps(clean_args),
                workdir,
                (risk or "").strip() or None,
            ),
        )
        self.conn.commit()
        return int(cursor.lastrowid)

    def latest(
        self, suggestion_id: int, *, statuses: tuple[str, ...] = ("proposed",)
    ) -> ActionRow | None:
        placeholders = ",".join("?" for _ in statuses)
        row = self.conn.execute(
            f"SELECT * FROM actions WHERE suggestion_id = ? "
            f"AND status IN ({placeholders}) ORDER BY id DESC LIMIT 1",
            (suggestion_id, *statuses),
        ).fetchone()
        return None if row is None else _row(row)

    def by_id(self, action_id: int) -> ActionRow | None:
        row = self.conn.execute(
            "SELECT * FROM actions WHERE id = ?", (action_id,)
        ).fetchone()
        return None if row is None else _row(row)

    def recent(self, limit: int = 20) -> tuple[ActionRow, ...]:
        rows = self.conn.execute(
            "SELECT * FROM actions ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return tuple(_row(row) for row in rows)

    def execute(self, action_id: int, *, confirmation: str) -> ActionRow:
        if not self.config.enabled:
            raise ActionError("actions are disabled in config")
        if confirmation != CONFIRMATION:
            raise ActionError(f"type {CONFIRMATION} exactly to approve this action")
        row = self.by_id(action_id)
        if row is None:
            raise ActionError(f"action {action_id} not found")
        if row.status != "proposed":
            raise ActionError(f"action {action_id} is {row.status}, not proposed")

        resolved = self._validate_program(row.program)
        workdir = self._validate_cwd(row.cwd)
        self.conn.execute(
            "UPDATE actions SET status = 'running', approved_at = ?, started_at = ? "
            "WHERE id = ?",
            (utc_now(), utc_now(), action_id),
        )
        self.conn.commit()
        try:
            result = subprocess.run(
                [resolved, *row.args],
                cwd=workdir or str(self.root),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.config.timeout_seconds,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                check=False,
            )
            output = (result.stdout or "") + (result.stderr or "")
            status = "succeeded" if result.returncode == 0 else "failed"
            exit_code = int(result.returncode)
            error = None if result.returncode == 0 else f"exit code {exit_code}"
        except subprocess.TimeoutExpired as exc:
            output = _text(exc.stdout) + _text(exc.stderr)
            status = "failed"
            exit_code = None
            error = f"timed out after {self.config.timeout_seconds}s"
        except OSError as exc:
            output = ""
            status = "failed"
            exit_code = None
            error = f"{type(exc).__name__}: {exc}"

        output = output[: self.config.max_output_chars]
        self.conn.execute(
            "UPDATE actions SET status = ?, finished_at = ?, exit_code = ?, "
            "output = ?, error = ? WHERE id = ?",
            (status, utc_now(), exit_code, output, error, action_id),
        )
        self.conn.commit()
        logger.info("action %d %s: %s %s", action_id, status, row.program, row.args)
        return self.by_id(action_id)  # type: ignore[return-value]

    def _validate_program(self, program: str) -> str:
        name = Path(program).name.casefold()
        if not name or program.casefold() != name:
            raise ActionError("program must be a bare executable name, not a path")
        stem = Path(name).stem.casefold()
        allowed = {Path(item).stem.casefold() for item in self.config.allowed_programs}
        if stem not in allowed:
            raise ActionError(
                f"program '{program}' is not in actions.allowed_programs"
            )
        resolved = shutil.which(name) or shutil.which(stem)
        if not resolved:
            raise ActionError(f"program '{program}' was not found on PATH")
        return resolved

    def _validate_args(self, args: list[str]) -> list[str]:
        if not isinstance(args, list) or len(args) > 20:
            raise ActionError("args must be a list of at most 20 strings")
        clean: list[str] = []
        for value in args:
            if not isinstance(value, str):
                raise ActionError("every action argument must be a string")
            if "\x00" in value or len(value) > 1000:
                raise ActionError("action argument is invalid or too long")
            clean.append(value)
        return clean

    def _validate_cwd(self, cwd: str | None) -> str | None:
        if cwd is None or not str(cwd).strip():
            return None
        path = Path(cwd).expanduser().resolve()
        if path != self.root and self.root not in path.parents:
            raise ActionError("action working directory must stay under the app root")
        if not path.is_dir():
            raise ActionError(f"action working directory not found: {path}")
        return str(path)


def _row(row: sqlite3.Row) -> ActionRow:
    try:
        args = tuple(str(value) for value in json.loads(row["args"] or "[]"))
    except (TypeError, ValueError, json.JSONDecodeError):
        args = ()
    return ActionRow(
        id=int(row["id"]),
        suggestion_id=int(row["suggestion_id"]),
        created=str(row["created"]),
        summary=str(row["summary"]),
        program=str(row["program"]),
        args=args,
        cwd=row["cwd"],
        risk=row["risk"],
        status=str(row["status"]),
        exit_code=row["exit_code"],
        output=row["output"],
        error=row["error"],
    )


def _text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)
