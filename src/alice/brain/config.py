"""Brain timing configuration."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..core.config import ConfigError, as_mapping, get_integer, reject_unknown


@dataclass(frozen=True)
class Triage:
    interval_minutes: int = 5
    max_batch_tokens: int = 3000


@dataclass(frozen=True)
class Consolidation:
    hour: int = 3
    max_events: int = 500
    max_tokens: int = 6000


@dataclass(frozen=True)
class BrainConfig:
    triage: Triage
    consolidation: Consolidation


def parse_brain(triage_raw: Any, consolidation_raw: Any = None) -> BrainConfig:
    mapping = as_mapping(triage_raw or {}, "triage")
    reject_unknown(mapping, {"interval_minutes", "max_batch_tokens"}, "triage")
    interval = get_integer(mapping, "interval_minutes", "triage", default=5)
    batch_tokens = get_integer(mapping, "max_batch_tokens", "triage", default=3000)
    if interval <= 0:
        raise ConfigError("triage.interval_minutes must be greater than zero")
    if batch_tokens <= 0:
        raise ConfigError("triage.max_batch_tokens must be greater than zero")

    night = as_mapping(consolidation_raw or {}, "consolidation")
    reject_unknown(night, {"hour", "max_events", "max_tokens"}, "consolidation")
    hour = get_integer(night, "hour", "consolidation", default=3)
    max_events = get_integer(night, "max_events", "consolidation", default=500)
    max_tokens = get_integer(night, "max_tokens", "consolidation", default=6000)
    if not 0 <= hour <= 23:
        raise ConfigError("consolidation.hour must be between 0 and 23")
    if max_events <= 0:
        raise ConfigError("consolidation.max_events must be greater than zero")
    if max_tokens <= 0:
        raise ConfigError("consolidation.max_tokens must be greater than zero")

    return BrainConfig(
        triage=Triage(interval_minutes=interval, max_batch_tokens=batch_tokens),
        consolidation=Consolidation(
            hour=hour, max_events=max_events, max_tokens=max_tokens
        ),
    )
