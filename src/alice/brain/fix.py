"""Draft a single fix plan for a suggestion, without executing it."""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from ..contracts.models import Message, ModelRouter
from .json_output import OutputFormatError, extract_object
from .prompts import SYSTEM_PROMPT_FIX

logger = logging.getLogger(__name__)


class FixFormatError(ValueError):
    """The model did not return a usable fix plan."""


@dataclass(frozen=True)
class FixPlan:
    summary: str
    program: str | None
    args: tuple[str, ...] = ()
    cwd: str | None = None
    risk: str = "medium"
    manual_steps: tuple[str, ...] = ()


class FixPlanner:
    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        router: ModelRouter,
        allowed_programs: Sequence[str],
        max_tokens: int = 500,
    ) -> None:
        self.conn = conn
        self.router = router
        self.allowed = tuple(
            Path(item).stem.casefold() for item in allowed_programs
        )
        self.max_tokens = max_tokens

    def plan(self, suggestion_id: int) -> FixPlan:
        row = self.conn.execute(
            "SELECT * FROM suggestions WHERE id = ? AND status != 'merged'",
            (suggestion_id,),
        ).fetchone()
        if row is None:
            raise FixFormatError(f"suggestion {suggestion_id} not found")
        context = [
            f"<suggestion id=\"{row['id']}\" priority=\"P{row['priority']}\">",
            f"text: {row['text']}",
        ]
        if row["reason"]:
            context.append(f"reason: {row['reason']}")
        if row["steps"]:
            context.append(f"steps: {row['steps']}")
        if row["evidence"]:
            context.append(f"evidence: {row['evidence']}")
        context.append("</suggestion>")
        messages = [
            Message("system", SYSTEM_PROMPT_FIX),
            Message(
                "user",
                "\n".join(context)
                + "\n\nAllowed programs: "
                + ", ".join(self.allowed),
            ),
        ]
        completion = self.router.complete(
            "reason",
            messages,
            self.max_tokens,
            purpose="fix-plan",
        )
        return parse_fix_plan(completion.text, self.allowed)


def parse_fix_plan(text: str, allowed: Sequence[str] = ()) -> FixPlan:
    try:
        payload = extract_object(text)
    except OutputFormatError as exc:
        raise FixFormatError(str(exc)) from exc

    summary = payload.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        raise FixFormatError("summary must be a non-empty string")
    if len(summary) > 400:
        raise FixFormatError("summary is too long")

    program = payload.get("program")
    if program is not None and not isinstance(program, str):
        raise FixFormatError("program must be a string or null")
    if isinstance(program, str):
        program = program.strip() or None
    if program is not None:
        name = Path(program).name.casefold()
        if program.casefold() != name:
            raise FixFormatError("program must be a bare executable name")
        if allowed and Path(name).stem.casefold() not in set(allowed):
            raise FixFormatError(
                f"program '{program}' is not in actions.allowed_programs"
            )

    raw_args = payload.get("args", [])
    if raw_args is None:
        raw_args = []
    if not isinstance(raw_args, list) or len(raw_args) > 20:
        raise FixFormatError("args must be a list of at most 20 strings")
    args: list[str] = []
    for index, value in enumerate(raw_args):
        if not isinstance(value, str) or "\x00" in value or len(value) > 1000:
            raise FixFormatError(f"args[{index}] is invalid")
        args.append(value)

    cwd = payload.get("cwd")
    if cwd is not None and not isinstance(cwd, str):
        raise FixFormatError("cwd must be a string or null")
    if isinstance(cwd, str):
        cwd = cwd.strip() or None

    risk = payload.get("risk", "medium")
    if risk not in ("low", "medium", "high"):
        raise FixFormatError("risk must be low, medium, or high")

    raw_steps = payload.get("manual_steps", [])
    if raw_steps is None:
        raw_steps = []
    if not isinstance(raw_steps, list) or len(raw_steps) > 8:
        raise FixFormatError("manual_steps must be a list of at most 8 strings")
    steps: list[str] = []
    for index, value in enumerate(raw_steps):
        if not isinstance(value, str) or not value.strip():
            raise FixFormatError(f"manual_steps[{index}] is invalid")
        steps.append(value.strip())

    return FixPlan(
        summary=summary.strip(),
        program=program,
        args=tuple(args),
        cwd=cwd,
        risk=risk,
        manual_steps=tuple(steps),
    )
