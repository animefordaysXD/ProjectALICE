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
class BrainConfig:
    triage: Triage


def parse_brain(raw: Any) -> BrainConfig:
    mapping = as_mapping(raw or {}, "triage")
    reject_unknown(mapping, {"interval_minutes", "max_batch_tokens"}, "triage")
    interval = get_integer(mapping, "interval_minutes", "triage", default=5)
    batch_tokens = get_integer(mapping, "max_batch_tokens", "triage", default=3000)
    if interval <= 0:
        raise ConfigError("triage.interval_minutes must be greater than zero")
    if batch_tokens <= 0:
        raise ConfigError("triage.max_batch_tokens must be greater than zero")
    return BrainConfig(triage=Triage(interval_minutes=interval, max_batch_tokens=batch_tokens))
