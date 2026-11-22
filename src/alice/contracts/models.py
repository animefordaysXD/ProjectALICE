"""Model-facing values and the router protocol."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, Sequence

ROLES = ("system", "user", "assistant")


@dataclass(frozen=True)
class Message:
    role: str
    content: str

    def __post_init__(self) -> None:
        if self.role not in ROLES:
            raise ValueError(f"invalid message role: {self.role!r}")
        if not isinstance(self.content, str):
            raise TypeError("message content must be text")


@dataclass(frozen=True)
class Completion:
    text: str
    input_tokens: int
    output_tokens: int
    cached_tokens: int = 0
    cost_usd: float = 0.0


class ModelRouter(Protocol):
    """What `brain` is allowed to know about model access."""

    def complete(
        self,
        tier: str,
        messages: Sequence[Message],
        max_tokens: int,
        *,
        purpose: str | None = None,
        cache_prefix_len: int | None = None,
    ) -> Completion: ...
