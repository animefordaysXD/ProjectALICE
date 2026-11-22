"""Plugin selection configuration."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..core.config import ConfigError, as_mapping, reject_unknown


@dataclass(frozen=True)
class PluginsConfig:
    enabled: tuple[str, ...] = ()


def parse_plugins(raw: Any) -> PluginsConfig:
    mapping = as_mapping(raw or {}, "plugins")
    reject_unknown(mapping, {"enabled"}, "plugins")
    enabled = mapping.get("enabled", [])
    if not isinstance(enabled, list):
        raise ConfigError("plugins.enabled must be a list")
    names: list[str] = []
    for index, value in enumerate(enabled):
        if not isinstance(value, str) or not value.strip():
            raise ConfigError(f"plugins.enabled[{index}]: expected a non-empty string")
        if value in names:
            raise ConfigError(f"plugins.enabled: duplicate plugin '{value}'")
        names.append(value)
    return PluginsConfig(enabled=tuple(names))
