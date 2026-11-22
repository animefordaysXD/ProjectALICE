"""Suggestion and notification contracts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class Suggestion:
    text: str
    priority: int = 3
    source_event_ids: tuple[int, ...] = ()
    reason: str | None = None

    def __post_init__(self) -> None:
        if not 1 <= self.priority <= 3:
            raise ValueError("priority must be between 1 and 3")
        if not self.text.strip():
            raise ValueError("suggestion text must not be empty")


@dataclass(frozen=True)
class BudgetSnapshot:
    paused: str | None
    calls_today: int
    spend_today: float
    soft_cap: float
    hard_cap: float
    alerts: tuple[str, ...] = ()


class SuggestionSink(Protocol):
    def submit(self, suggestion: Suggestion) -> int: ...


class Notifier(Protocol):
    def notify(self, title: str, body: str) -> bool:
        """Deliver a toast. Returns False when delivery is unavailable."""
