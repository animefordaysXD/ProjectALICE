"""Provider interface.

Providers are swappable. They know how to talk to a model, count tokens, and
report usage; they do not know about budgets, tiers, or Alice's ledger.

`Message` and `Completion` live in `alice.contracts.models` because they cross
module boundaries (brain -> router -> provider). Re-exported here so provider
implementations and tests have one natural import.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Sequence

from ..contracts.models import Completion, Message

__all__ = [
    "Completion",
    "Message",
    "Provider",
    "ProviderError",
    "ProviderNotConfigured",
]


class ProviderError(RuntimeError):
    """A provider call failed. Transient errors are retried once."""

    def __init__(self, message: str, *, transient: bool = False) -> None:
        super().__init__(message)
        self.transient = transient


class ProviderNotConfigured(RuntimeError):
    """The provider cannot be constructed (missing package, key, or URL)."""


class Provider(ABC):
    name: str
    # True when the provider reports the call's dollar cost itself (a free or
    # subscription provider). When False, config pricing is mandatory.
    reports_cost: bool = False

    @abstractmethod
    def complete(
        self,
        messages: Sequence[Message],
        model: str,
        max_tokens: int,
        cache_prefix_len: int | None = None,
    ) -> Completion:
        """Send one request and return text plus token accounting."""
