"""Memory facade used by the app and, later, by the brain.

Combines the editable core-memory JSON, approved hard memory, task scratchpad,
and archive recall behind one small API.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from ..contracts.memory import Embedder, Recall
from .archive import Archive
from .config import MemoryConfig
from .core_file import CoreMemory
from .errors import MemoryCapError
from .fast import FastMemory, Task
from .hard import ApprovedEntry, HardMemory, Proposal, render_entries
from ..core.tokens import estimate_tokens


def _utc() -> datetime:
    return datetime.now(timezone.utc)


class MemoryService:
    def __init__(
        self,
        conn: sqlite3.Connection,
        config: MemoryConfig,
        *,
        embedder: Embedder | None = None,
        sanitize: Callable[[str], str] | None = None,
        clock: Callable[[], datetime] = _utc,
    ) -> None:
        self.conn = conn
        self.config = config
        self.core = CoreMemory(config.core_memory_file, config.hard_max_tokens)
        self.hard = HardMemory(conn)
        self.fast = FastMemory(
            conn,
            ttl_hours=config.task_ttl_hours,
            sanitize=sanitize,
            clock=clock,
        )
        self.archive = Archive(conn, config, embedder=embedder)

    @property
    def core_memory_file(self):
        return self.core.path

    def ensure(self) -> bool:
        return self.core.ensure()

    def hard_block(self) -> str:
        core_text = self.core.render()
        approved = self.hard.render()
        text = "\n\n".join(part for part in (core_text, approved) if part)
        tokens = estimate_tokens(text)
        if tokens > self.config.hard_max_tokens:
            raise MemoryCapError(
                f"hard memory is ~{tokens} tokens, over the "
                f"{self.config.hard_max_tokens}-token cap; prune "
                f"{self.core.path} or approved memory"
            )
        return text

    def core_tokens(self) -> int:
        return estimate_tokens(self.core.render()) if self.core.exists() else 0

    def propose(self, key: str, value: str, reason: str = "") -> int:
        return self.hard.propose(key, value, reason)

    def pending(self) -> list[Proposal]:
        return self.hard.pending()

    def approve(self, proposal_id: int) -> ApprovedEntry:
        proposal = self.hard.get_pending(proposal_id)
        core_text = self.core.render()
        entries = {entry.key: entry.value for entry in self.hard.active()}
        entries[proposal.key] = proposal.proposed_value
        rendered = render_entries(entries)
        tokens = estimate_tokens("\n\n".join(part for part in (core_text, rendered) if part))
        if tokens > self.config.hard_max_tokens:
            raise MemoryCapError(
                f"approving proposal {proposal_id} would make hard memory ~{tokens} "
                f"tokens, over the {self.config.hard_max_tokens}-token cap; "
                "prune core memory first"
            )
        return self.hard.approve(proposal_id)

    def reject(self, proposal_id: int) -> None:
        self.hard.reject(proposal_id)

    def recall(
        self, query: str, k: int | None = None, since: str | None = None
    ) -> Recall:
        return self.archive.recall(query, k=k, since=since)

    def index_pending(self, limit: int = 100) -> int:
        return self.archive.index_pending(limit=limit)

    def create_task(self, goal: str, state: Mapping[str, Any] | None = None) -> Task:
        return self.fast.create(goal, state)

    def complete_task(self, task_id: int, outcome: str) -> Task | None:
        return self.fast.complete(task_id, outcome)

    def expire_tasks(self) -> list[int]:
        return self.fast.expire_due()

    def open_tasks(self) -> int:
        return self.fast.open_count()

    def record_summary(
        self, level: str, period_start: str, period_end: str, text: str
    ) -> int:
        cursor = self.conn.execute(
            """
            INSERT INTO summaries (level, period_start, period_end, text)
            VALUES (?, ?, ?, ?)
            """,
            (level, period_start, period_end, text),
        )
        self.conn.commit()
        return int(cursor.lastrowid)
