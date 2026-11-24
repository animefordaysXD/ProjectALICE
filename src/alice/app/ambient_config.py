"""Configuration for passive calendar and foreground-activity monitoring."""

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
class AmbientConfig:
    enabled: bool = True
    calendar_lookahead_minutes: int = 30
    focus_minutes: int = 90
    idle_minutes: int = 30
    switch_window_minutes: int = 30
    switch_threshold: int = 20
    late_night_hour: int = 1
    max_alerts_per_hour: int = 2


def parse_ambient(raw: Any) -> AmbientConfig:
    mapping = as_mapping(raw or {}, "ambient")
    reject_unknown(
        mapping,
        {
            "enabled",
            "calendar_lookahead_minutes",
            "focus_minutes",
            "idle_minutes",
            "switch_window_minutes",
            "switch_threshold",
            "late_night_hour",
            "max_alerts_per_hour",
        },
        "ambient",
    )
    enabled = get_boolean(mapping, "enabled", "ambient", default=True)
    lookahead = get_integer(
        mapping, "calendar_lookahead_minutes", "ambient", default=30
    )
    if not 1 <= lookahead <= 240:
        raise ConfigError(
            "ambient.calendar_lookahead_minutes must be between 1 and 240"
        )
    focus = get_integer(mapping, "focus_minutes", "ambient", default=90)
    if not 10 <= focus <= 600:
        raise ConfigError("ambient.focus_minutes must be between 10 and 600")
    idle = get_integer(mapping, "idle_minutes", "ambient", default=30)
    if not 5 <= idle <= 600:
        raise ConfigError("ambient.idle_minutes must be between 5 and 600")
    switch_window = get_integer(
        mapping, "switch_window_minutes", "ambient", default=30
    )
    if not 5 <= switch_window <= 240:
        raise ConfigError(
            "ambient.switch_window_minutes must be between 5 and 240"
        )
    switch_threshold = get_integer(
        mapping, "switch_threshold", "ambient", default=20
    )
    if not 5 <= switch_threshold <= 100:
        raise ConfigError("ambient.switch_threshold must be between 5 and 100")
    late_night = get_integer(
        mapping, "late_night_hour", "ambient", default=1
    )
    if not 0 <= late_night <= 6:
        raise ConfigError("ambient.late_night_hour must be between 0 and 6")
    max_alerts = get_integer(
        mapping, "max_alerts_per_hour", "ambient", default=2
    )
    if not 1 <= max_alerts <= 10:
        raise ConfigError("ambient.max_alerts_per_hour must be between 1 and 10")
    return AmbientConfig(
        enabled=enabled,
        calendar_lookahead_minutes=lookahead,
        focus_minutes=focus,
        idle_minutes=idle,
        switch_window_minutes=switch_window,
        switch_threshold=switch_threshold,
        late_night_hour=late_night,
        max_alerts_per_hour=max_alerts,
    )
