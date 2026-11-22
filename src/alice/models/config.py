"""Model tier, pricing, and budget configuration."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any

from ..core.config import (
    ConfigError,
    as_mapping,
    freeze,
    get_boolean,
    get_integer,
    get_number,
    get_string,
    reject_unknown,
)

PROVIDERS = ("anthropic", "fake", "opencode")
REQUIRED_TIERS = ("triage", "reason", "consolidate")


@dataclass(frozen=True)
class Pricing:
    """USD per million tokens, as configured by Jo."""

    input_per_mtok: float
    output_per_mtok: float
    cached_input_per_mtok: float | None = None


@dataclass(frozen=True)
class ModelSpec:
    provider: str
    model: str
    batch: bool = False
    pricing: Pricing | None = None


@dataclass(frozen=True)
class Budget:
    soft_daily_usd: float = 1.00
    hard_daily_usd: float = 2.50
    max_escalations_per_hour: int = 6
    max_heavy_per_day: int = 3


@dataclass(frozen=True)
class ModelsConfig(Mapping[str, ModelSpec]):
    """Tier map plus the budget that governs every call."""

    tiers: Mapping[str, ModelSpec]
    budget: Budget

    def __getitem__(self, key: str) -> ModelSpec:
        return self.tiers[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self.tiers)

    def __len__(self) -> int:
        return len(self.tiers)


def parse_models(models_raw: Any, budget_raw: Any) -> ModelsConfig:
    if models_raw is None:
        raise ConfigError("missing required section: models")
    raw = as_mapping(models_raw, "models")

    tiers: dict[str, ModelSpec] = {}
    for tier, value in raw.items():
        if not isinstance(tier, str) or not tier:
            raise ConfigError("models: tier names must be non-empty strings")
        tiers[tier] = _model_spec(tier, value)
    missing = [tier for tier in REQUIRED_TIERS if tier not in tiers]
    if missing:
        raise ConfigError(f"models: missing required tier(s): {', '.join(missing)}")

    budget = _budget(as_mapping(budget_raw or {}, "budget"))
    return ModelsConfig(tiers=freeze(tiers), budget=budget)


def _model_spec(tier: str, value: Any) -> ModelSpec:
    mapping = as_mapping(value, f"models.{tier}")
    provider = get_string(mapping, "provider", f"models.{tier}")
    model = get_string(mapping, "model", f"models.{tier}")
    batch = get_boolean(mapping, "batch", f"models.{tier}", default=False)
    if provider not in PROVIDERS:
        raise ConfigError(
            f"models.{tier}.provider: unknown provider '{provider}' "
            f"(supported: {', '.join(PROVIDERS)})"
        )
    pricing = _pricing(mapping.get("pricing"), f"models.{tier}.pricing")
    reject_unknown(mapping, {"provider", "model", "batch", "pricing"}, f"models.{tier}")
    return ModelSpec(provider=provider, model=model, batch=batch, pricing=pricing)


def _pricing(value: Any, where: str) -> Pricing | None:
    if value is None:
        return None
    mapping = as_mapping(value, where)
    reject_unknown(
        mapping, {"input_per_mtok", "output_per_mtok", "cached_input_per_mtok"}, where
    )
    missing = [key for key in ("input_per_mtok", "output_per_mtok") if key not in mapping]
    if missing:
        raise ConfigError(f"{where}: missing required key(s): {', '.join(missing)}")
    input_price = get_number(mapping, "input_per_mtok", where, default=0.0)
    output_price = get_number(mapping, "output_per_mtok", where, default=0.0)
    if input_price < 0 or output_price < 0:
        raise ConfigError(f"{where}: prices must not be negative")
    cached = None
    if "cached_input_per_mtok" in mapping:
        cached = get_number(mapping, "cached_input_per_mtok", where, default=0.0)
        if cached < 0:
            raise ConfigError(f"{where}.cached_input_per_mtok must not be negative")
    return Pricing(
        input_per_mtok=input_price,
        output_per_mtok=output_price,
        cached_input_per_mtok=cached,
    )


def _budget(raw: Mapping[str, Any]) -> Budget:
    reject_unknown(
        raw,
        {"soft_daily_usd", "hard_daily_usd", "max_escalations_per_hour", "max_heavy_per_day"},
        "budget",
    )
    soft = get_number(raw, "soft_daily_usd", "budget", default=1.00)
    hard = get_number(raw, "hard_daily_usd", "budget", default=2.50)
    escalations = get_integer(raw, "max_escalations_per_hour", "budget", default=6)
    heavy = get_integer(raw, "max_heavy_per_day", "budget", default=3)
    if soft <= 0 or hard <= 0:
        raise ConfigError("budget: daily caps must be greater than zero")
    if soft > hard:
        raise ConfigError("budget: soft_daily_usd must not exceed hard_daily_usd")
    if escalations < 0 or heavy < 0:
        raise ConfigError("budget: hourly/daily limits must not be negative")
    return Budget(
        soft_daily_usd=soft,
        hard_daily_usd=hard,
        max_escalations_per_hour=escalations,
        max_heavy_per_day=heavy,
    )
