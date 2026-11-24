"""Configuration for supervised, read-only PC roaming leases."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..core.config import (
    ConfigError,
    as_mapping,
    get_boolean,
    get_integer,
    reject_unknown,
)


@dataclass(frozen=True)
class RoamConfig:
    enabled: bool = True
    idle_minutes: int = 5
    step_seconds: int = 300
    max_steps: int = 500
    memory_seconds: int = 600
    max_suggestions: int = 3
    allow_when_active: bool = False


def parse_roam(raw: Any) -> RoamConfig:
    mapping = as_mapping(raw or {}, "roam")
    reject_unknown(
        mapping,
        {
            "enabled",
            "idle_minutes",
            "step_seconds",
            "max_steps",
            "memory_seconds",
            "max_suggestions",
            "allow_when_active",
        },
        "roam",
    )
    enabled = get_boolean(mapping, "enabled", "roam", default=True)
    idle = get_integer(mapping, "idle_minutes", "roam", default=5)
    if not 1 <= idle <= 120:
        raise ConfigError("roam.idle_minutes must be between 1 and 120")
    step = get_integer(mapping, "step_seconds", "roam", default=300)
    if not 5 <= step <= 3600:
        raise ConfigError("roam.step_seconds must be between 5 and 3600")
    max_steps = get_integer(mapping, "max_steps", "roam", default=500)
    if not 1 <= max_steps <= 5000:
        raise ConfigError("roam.max_steps must be between 1 and 5000")
    memory = get_integer(mapping, "memory_seconds", "roam", default=600)
    if not 60 <= memory <= 7200:
        raise ConfigError("roam.memory_seconds must be between 60 and 7200")
    suggestions = get_integer(mapping, "max_suggestions", "roam", default=3)
    if not 0 <= suggestions <= 20:
        raise ConfigError("roam.max_suggestions must be between 0 and 20")
    allow_active = get_boolean(
        mapping, "allow_when_active", "roam", default=False
    )
    return RoamConfig(
        enabled=enabled,
        idle_minutes=idle,
        step_seconds=step,
        max_steps=max_steps,
        memory_seconds=memory,
        max_suggestions=suggestions,
        allow_when_active=allow_active,
    )
