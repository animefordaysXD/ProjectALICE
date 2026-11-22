"""Tiered model router.

Tiers come from config only. Every call passes the budget guard, retries once
on a transient provider error, gets priced, and is written to `usage` before
the result is returned.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import replace
from pathlib import Path
from typing import Callable, Mapping, Sequence

from .config import ModelsConfig, ModelSpec, Pricing
from .anthropic import AnthropicProvider
from .base import Completion, Message, Provider, ProviderError
from .budget import BudgetGuard, UsageLog
from .fake import FakeProvider
from .opencode import OpenCodeProvider

logger = logging.getLogger(__name__)


class RouterError(RuntimeError):
    """The request cannot be routed, priced, or attempted safely."""


def estimate_input_tokens(messages: Sequence[Message]) -> int:
    """Pessimistic input estimate: one token per character.

    English is closer to four characters per token and Korean closer to one,
    so this deliberately overestimates to protect the hard cap.
    """
    return max(1, sum(len(message.content) for message in messages))


def estimate_cost(
    messages: Sequence[Message], max_tokens: int, pricing: Pricing
) -> float:
    return (
        estimate_input_tokens(messages) * pricing.input_per_mtok
        + max_tokens * pricing.output_per_mtok
    ) / 1_000_000


def completion_cost(completion: Completion, pricing: Pricing) -> float:
    cached = min(max(completion.cached_tokens, 0), completion.input_tokens)
    uncached = max(completion.input_tokens - cached, 0)
    cached_price = (
        pricing.cached_input_per_mtok
        if pricing.cached_input_per_mtok is not None
        else pricing.input_per_mtok
    )
    return (
        uncached * pricing.input_per_mtok
        + cached * cached_price
        + completion.output_tokens * pricing.output_per_mtok
    ) / 1_000_000


class Router:
    def __init__(
        self,
        *,
        models: Mapping[str, ModelSpec],
        providers: Mapping[str, Provider],
        usage: UsageLog,
        budget: BudgetGuard,
        sleep: Callable[[float], None] = time.sleep,
        retry_delay: float = 1.0,
    ) -> None:
        self.models = models
        self.providers = providers
        self.usage = usage
        self.budget = budget
        self.sleep = sleep
        self.retry_delay = retry_delay

    def complete(
        self,
        tier: str,
        messages: Sequence[Message],
        max_tokens: int,
        *,
        purpose: str | None = None,
        cache_prefix_len: int | None = None,
    ) -> Completion:
        spec = self.models.get(tier)
        if spec is None:
            raise RouterError(f"unknown model tier: {tier!r}")
        if max_tokens <= 0:
            raise RouterError("max_tokens must be positive")

        provider = self.providers.get(spec.provider)
        if provider is None:
            raise RouterError(
                f"provider '{spec.provider}' for tier '{tier}' is not available"
            )

        if spec.pricing is None:
            if not provider.reports_cost:
                raise RouterError(
                    f"tier '{tier}' has no pricing in config; refusing to make an "
                    "unpriced paid call"
                )
            estimate = 0.0
        else:
            estimate = estimate_cost(messages, max_tokens, spec.pricing)

        self.budget.check(tier=tier, estimated_usd=estimate)

        completion = self._call_with_retry(
            provider, tier, messages, spec.model, max_tokens, cache_prefix_len
        )
        cost = (
            completion_cost(completion, spec.pricing)
            if spec.pricing
            else completion.cost_usd
        )
        completion = replace(completion, cost_usd=cost)

        self.usage.record(
            tier=tier,
            model=spec.model,
            input_tokens=completion.input_tokens,
            cached_tokens=completion.cached_tokens,
            output_tokens=completion.output_tokens,
            cost_usd=cost,
            purpose=purpose,
        )
        self.budget.note_spend(cost)
        return completion

    def _call_with_retry(
        self,
        provider: Provider,
        tier: str,
        messages: Sequence[Message],
        model: str,
        max_tokens: int,
        cache_prefix_len: int | None,
    ) -> Completion:
        attempts = 2
        for attempt in range(1, attempts + 1):
            try:
                return provider.complete(
                    messages, model, max_tokens, cache_prefix_len
                )
            except ProviderError as exc:
                if not exc.transient or attempt == attempts:
                    raise
                logger.warning(
                    "transient provider failure (tier=%s attempt=%d); retrying once",
                    tier,
                    attempt,
                )
                self.sleep(self.retry_delay)
        raise AssertionError("unreachable")


def build_router(
    models: ModelsConfig, *, data_dir: Path, conn: sqlite3.Connection
) -> Router:
    """Build a router for the configured tiers. Anthropic needs its API key."""
    providers: dict[str, Provider] = {}
    for spec in models.tiers.values():
        if spec.provider in providers:
            continue
        if spec.provider == "anthropic":
            providers[spec.provider] = AnthropicProvider()
        elif spec.provider == "opencode":
            workspace = Path(data_dir) / "opencode"
            workspace.mkdir(parents=True, exist_ok=True)
            providers[spec.provider] = OpenCodeProvider(cwd=workspace)
        elif spec.provider == "fake":
            providers[spec.provider] = FakeProvider()
        else:  # config validation should make this unreachable
            raise RouterError(f"unknown provider in config: {spec.provider}")

    return Router(
        models=models.tiers,
        providers=providers,
        usage=UsageLog(conn),
        budget=BudgetGuard(conn, models.budget, data_dir),
    )
