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
    steps: tuple[str, ...] = ()
    evidence: str | None = None
    category: str | None = None
    confidence: float | None = None

    def __post_init__(self) -> None:
        if not 1 <= self.priority <= 3:
            raise ValueError("priority must be between 1 and 3")
        if not self.text.strip():
            raise ValueError("suggestion text must not be empty")
        if len(self.steps) > 6:
            raise ValueError("at most 6 suggestion steps are allowed")
        if any(not step.strip() for step in self.steps):
            raise ValueError("suggestion steps must not be blank")
        if self.confidence is not None and not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence must be between 0 and 1")


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
