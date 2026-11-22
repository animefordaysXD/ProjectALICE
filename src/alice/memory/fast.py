"""Fast memory: one scratchpad per task, with TTL and an archived outcome."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping

from ..contracts.memory import Task
from ..core.events import Ledger
from .errors import ProposalError

Clock = Callable[[], datetime]


def _utc() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class FastMemory:
    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        ttl_hours: int = 48,
        ledger: Ledger | None = None,
        sanitize: Callable[[str], str] | None = None,
        clock: Clock = _utc,
    ) -> None:
        self.conn = conn
        self.ttl_hours = ttl_hours
        self.ledger = ledger or Ledger(conn)
        self.sanitize = sanitize or (lambda text: text)
        self.clock = clock

    def create(self, goal: str, state: Mapping[str, Any] | None = None) -> Task:
        if not goal.strip():
            raise ProposalError("task goal must not be empty")
        now = self.clock()
        created = _iso(now)
        expires = _iso(now + timedelta(hours=self.ttl_hours))
        cursor = self.conn.execute(
            "INSERT INTO tasks (goal, state, status, created, expires) VALUES (?, ?, 'open', ?, ?)",
            (goal.strip(), json.dumps(dict(state or {}), ensure_ascii=False), created, expires),
        )
        self.conn.commit()
        return Task(
            id=int(cursor.lastrowid),
            goal=goal.strip(),
            state=dict(state or {}),
            status="open",
            created=created,
            expires=expires,
        )

    def get(self, task_id: int) -> Task | None:
        row = self.conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return None if row is None else _task(row)

    def update_state(self, task_id: int, update: Mapping[str, Any]) -> Task:
        task = self.get(task_id)
        if task is None:
            raise ProposalError(f"task {task_id} not found")
        state = dict(task.state)
        state.update(update)
        self.conn.execute(
            "UPDATE tasks SET state = ? WHERE id = ?",
            (json.dumps(state, ensure_ascii=False), task_id),
        )
        self.conn.commit()
        return Task(
            id=task.id,
            goal=task.goal,
            state=state,
            status=task.status,
            created=task.created,
            expires=task.expires,
        )

    def complete(self, task_id: int, outcome: str) -> Task | None:
        task = self.get(task_id)
        if task is None:
            return None
        self._archive(task, "done", outcome, self.clock())
        self.conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        self.conn.commit()
        return task

    def expire_due(self, now: datetime | None = None) -> list[int]:
        moment = now or self.clock()
        stamp = _iso(moment)
        rows = self.conn.execute(
            "SELECT * FROM tasks WHERE status = 'open' AND expires <= ? ORDER BY expires",
            (stamp,),
        ).fetchall()
        expired: list[int] = []
        for row in rows:
            task = _task(row)
            self._archive(task, "expired", "TTL expired before completion", moment)
            self.conn.execute("DELETE FROM tasks WHERE id = ?", (task.id,))
            expired.append(task.id)
        self.conn.commit()
        return expired

    def open_count(self) -> int:
        return int(
            self.conn.execute("SELECT COUNT(*) FROM tasks WHERE status = 'open'").fetchone()[0]
        )

    def _archive(self, task: Task, status: str, outcome: str, moment: datetime) -> None:
        text = self.sanitize(
            f"task {task.id} {status}: {outcome.strip() or 'no outcome recorded'}"
        )
        stamp = _iso(moment)
        digest = hashlib.sha256(
            f"task_outcome:{task.id}:{status}:{stamp}".encode("utf-8")
        ).hexdigest()
        self.ledger.record(
            ts=stamp,
            source="memory:task",
            kind="task_outcome",
            content_hash=digest,
            text_redacted=text,
            raw_ref=None,
            salience=0.0,
            last_seen=stamp,
        )


def _task(row: sqlite3.Row) -> Task:
    try:
        state = json.loads(row["state"] or "{}")
    except json.JSONDecodeError:
        state = {}
    if not isinstance(state, dict):
        state = {"value": state}
    return Task(
        id=int(row["id"]),
        goal=str(row["goal"]),
        state=state,
        status=str(row["status"]),
        created=str(row["created"]),
        expires=str(row["expires"]),
    )
