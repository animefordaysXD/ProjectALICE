"""Memory values and the store protocol used by the brain."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol, Sequence

MEMORY_KINDS = ("hard", "task", "summary", "event")


@dataclass(frozen=True)
class MemoryItem:
    kind: str
    text: str
    score: float = 0.0
    key: str | None = None
    source_ids: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        if self.kind not in MEMORY_KINDS:
            raise ValueError(f"invalid memory kind: {self.kind!r}")


@dataclass(frozen=True)
class Recall:
    items: tuple[MemoryItem, ...] = ()
    used_tokens: int = 0
    truncated: bool = False


class MemoryStore(Protocol):
    """Reads available to the brain; writes go through explicit methods."""

    def hard_block(self) -> str: ...

    def recall(
        self, query: str, k: int = 5, since: str | None = None
    ) -> Recall: ...

    def record_summary(
        self, level: str, period_start: str, period_end: str, text: str
    ) -> int: ...

    def summary_exists(self, level: str, period_start: str) -> bool: ...

    def summary_text(self, level: str, period_start: str) -> str | None: ...

    def propose(self, key: str, value: str, reason: str) -> int: ...


class Embedder(Protocol):
    dimension: int

    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


@dataclass(frozen=True)
class Task:
    id: int
    goal: str
    state: Mapping[str, Any]
    status: str
    created: str
    expires: str


class TaskStore(Protocol):
    def create(
        self, goal: str, state: Mapping[str, Any] | None = None
    ) -> Task: ...

    def update_state(self, task_id: int, update: Mapping[str, Any]) -> Task: ...

    def complete(self, task_id: int, outcome: str) -> Task | None: ...
