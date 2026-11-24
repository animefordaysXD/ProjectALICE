"""Configuration for local screen-following suggestions."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..core.config import (
    ConfigError,
    as_mapping,
    get_boolean,
    get_integer,
    get_number,
    reject_unknown,
)


DEFAULT_EXCLUDED_APPS = (
    "keepass.exe",
    "keepassxc.exe",
    "1password.exe",
    "bitwarden.exe",
    "lastpass.exe",
    "dashlane.exe",
)


@dataclass(frozen=True)
class MonitorConfig:
    enabled: bool = True
    capture_seconds: int = 20
    analyze_seconds: int = 300
    idle_seconds: int = 300
    max_ocr_chars: int = 2500
    min_confidence: float = 0.5
    max_suggestions_per_hour: int = 2
    exclude_apps: tuple[str, ...] = field(default_factory=lambda: DEFAULT_EXCLUDED_APPS)
    exclude_titles: tuple[str, ...] = (
        "password",
        "incognito",
        "private browsing",
        "inprivate",
    )
    excerpt_chars: int = 300


def parse_monitor(raw: Any) -> MonitorConfig:
    mapping = as_mapping(raw or {}, "monitor")
    reject_unknown(
        mapping,
        {
            "enabled",
            "capture_seconds",
            "analyze_seconds",
            "idle_seconds",
            "max_ocr_chars",
            "min_confidence",
            "max_suggestions_per_hour",
            "exclude_apps",
            "exclude_titles",
            "excerpt_chars",
        },
        "monitor",
    )
    enabled = get_boolean(mapping, "enabled", "monitor", default=True)
    capture = get_integer(mapping, "capture_seconds", "monitor", default=20)
    if not 5 <= capture <= 600:
        raise ConfigError("monitor.capture_seconds must be between 5 and 600")
    analyze = get_integer(mapping, "analyze_seconds", "monitor", default=300)
    if not 30 <= analyze <= 3600:
        raise ConfigError("monitor.analyze_seconds must be between 30 and 3600")
    idle = get_integer(mapping, "idle_seconds", "monitor", default=300)
    if not 30 <= idle <= 3600:
        raise ConfigError("monitor.idle_seconds must be between 30 and 3600")
    ocr_chars = get_integer(mapping, "max_ocr_chars", "monitor", default=2500)
    if not 200 <= ocr_chars <= 8000:
        raise ConfigError("monitor.max_ocr_chars must be between 200 and 8000")
    confidence = get_number(mapping, "min_confidence", "monitor", default=0.5)
    if not 0.0 <= confidence <= 1.0:
        raise ConfigError("monitor.min_confidence must be between 0 and 1")
    per_hour = get_integer(
        mapping, "max_suggestions_per_hour", "monitor", default=2
    )
    if not 1 <= per_hour <= 6:
        raise ConfigError(
            "monitor.max_suggestions_per_hour must be between 1 and 6"
        )
    exclude_apps = _string_tuple(
        mapping.get("exclude_apps", list(DEFAULT_EXCLUDED_APPS)),
        "monitor.exclude_apps",
    )
    exclude_titles = _string_tuple(
        mapping.get(
            "exclude_titles",
            ["password", "incognito", "private browsing", "inprivate"],
        ),
        "monitor.exclude_titles",
    )
    excerpt_chars = get_integer(mapping, "excerpt_chars", "monitor", default=300)
    if not 0 <= excerpt_chars <= 1000:
        raise ConfigError("monitor.excerpt_chars must be between 0 and 1000")
    return MonitorConfig(
        enabled=enabled,
        capture_seconds=capture,
        analyze_seconds=analyze,
        idle_seconds=idle,
        max_ocr_chars=ocr_chars,
        min_confidence=confidence,
        max_suggestions_per_hour=per_hour,
        exclude_apps=exclude_apps,
        exclude_titles=exclude_titles,
        excerpt_chars=excerpt_chars,
    )


def _string_tuple(raw: Any, label: str) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ConfigError(f"{label} must be a list of strings")
    values: list[str] = []
    for index, value in enumerate(raw):
        if not isinstance(value, str) or not value.strip():
            raise ConfigError(f"{label}[{index}]: expected a non-empty string")
        values.append(value.strip().casefold())
    return tuple(values)
