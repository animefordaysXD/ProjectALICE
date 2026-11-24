"""Notification and digest configuration."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from ..core.config import ConfigError, as_mapping, get_integer, get_string, reject_unknown

NOTIFY_MODES = ("digest+toast", "digest", "toast")
_QUIET_HOURS_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d-([01]\d|2[0-3]):[0-5]\d$")


@dataclass(frozen=True)
class Notify:
    mode: str = "digest"
    quiet_hours: str = "22:00-08:00"
    max_toasts_per_hour: int = 3
    min_toast_priority: int = 2


@dataclass(frozen=True)
class OutputsConfig:
    notify: Notify


def parse_outputs(raw: Any) -> OutputsConfig:
    mapping = as_mapping(raw or {}, "notify")
    reject_unknown(
        mapping,
        {"mode", "quiet_hours", "max_toasts_per_hour", "min_toast_priority"},
        "notify",
    )
    mode = get_string(mapping, "mode", "notify", default="digest")
    if mode not in NOTIFY_MODES:
        raise ConfigError(f"notify.mode must be one of: {', '.join(NOTIFY_MODES)}")
    quiet_hours = get_string(mapping, "quiet_hours", "notify", default="22:00-08:00")
    if not _QUIET_HOURS_RE.match(quiet_hours):
        raise ConfigError("notify.quiet_hours must look like '22:00-08:00'")
    max_toasts = get_integer(mapping, "max_toasts_per_hour", "notify", default=3)
    if max_toasts < 0:
        raise ConfigError("notify.max_toasts_per_hour must not be negative")
    min_priority = get_integer(
        mapping, "min_toast_priority", "notify", default=2
    )
    if not 1 <= min_priority <= 3:
        raise ConfigError("notify.min_toast_priority must be 1, 2, or 3")
    return OutputsConfig(
        notify=Notify(
            mode=mode,
            quiet_hours=quiet_hours,
            max_toasts_per_hour=max_toasts,
            min_toast_priority=min_priority,
        )
    )
