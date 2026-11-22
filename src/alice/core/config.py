"""Shared configuration helpers and core settings.

Modules own their own sections; this file only provides the parser helpers,
`ConfigError`, and the core-owned settings (runtime paths + retention).
"""

from __future__ import annotations

import codecs
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

import yaml

DEFAULT_CONFIG_PATH = Path("config/alice.yaml")

_SECRET_KEYS = ("secret", "password", "credential", "credentials")


class ConfigError(ValueError):
    """Raised when a config file is missing, malformed, or unsafe."""


@dataclass(frozen=True)
class Retention:
    raw_payload_days: int = 14
    max_raw_mb: float = 50.0


@dataclass(frozen=True)
class CoreConfig:
    data_dir: Path
    seed_dir: Path
    retention: Retention


def default_config_path() -> Path:
    """Config next to the exe when frozen, else ./config/alice.yaml."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / "config" / "alice.yaml"
    return DEFAULT_CONFIG_PATH


def load_yaml(path: Path) -> Mapping[str, Any]:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {path}: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    if raw is None:
        raise ConfigError(f"{path} is empty")
    if not isinstance(raw, dict):
        raise ConfigError(f"{path} must contain a mapping at the top level")
    return raw


def require_section(
    raw: Mapping[str, Any],
    key: str,
    *,
    required: bool = False,
    default: Any = None,
) -> Mapping[str, Any]:
    if key not in raw:
        if required:
            raise ConfigError(f"missing required section: {key}")
        return default if default is not None else {}
    return as_mapping(raw[key], key)


def as_mapping(value: Any, where: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"{where}: expected a mapping")
    return value


def get_string(
    raw: Mapping[str, Any], key: str, where: str, *, default: str | None = None
) -> str:
    value = raw.get(key, default)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{where}.{key}: expected a non-empty string")
    return value.strip()


def get_number(
    raw: Mapping[str, Any], key: str, where: str, *, default: float
) -> float:
    value = raw.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{where}.{key}: expected a number")
    return float(value)


def get_integer(
    raw: Mapping[str, Any], key: str, where: str, *, default: int
) -> int:
    value = raw.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{where}.{key}: expected an integer")
    return value


def get_boolean(
    raw: Mapping[str, Any], key: str, where: str, *, default: bool
) -> bool:
    value = raw.get(key, default)
    if not isinstance(value, bool):
        raise ConfigError(f"{where}.{key}: expected true or false")
    return value


def compile_regex(
    raw: Mapping[str, Any], key: str, where: str
) -> re.Pattern[str]:
    value = get_string(raw, key, where)
    try:
        return re.compile(value)
    except re.error as exc:
        raise ConfigError(f"{where}.{key}: invalid regex: {exc}") from exc


def path_option(raw: Mapping[str, Any], key: str, root: Path) -> Path:
    value = raw.get(key)
    if value is None:
        return root
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{key} must be a non-empty path string")
    return resolve_path(Path(value), root)


def resolve_path(value: Path, base: Path) -> Path:
    candidate = value.expanduser()
    return candidate if candidate.is_absolute() else (base / candidate)


def reject_unknown(raw: Mapping[str, Any], allowed: set[str], where: str) -> None:
    unknown = sorted(set(map(str, raw)) - allowed)
    if unknown:
        raise ConfigError(f"{where}: unknown key(s): {', '.join(unknown)}")


def reject_secrets(node: Any, trail: str = "") -> None:
    """Refuse config files that carry credentials."""
    if isinstance(node, dict):
        for key, value in node.items():
            name = str(key).lower().replace("-", "_")
            where = f"{trail}.{key}" if trail else str(key)
            looks_secret = (
                name in _SECRET_KEYS
                or name == "token"
                or name.endswith(("_token", "_key", "_secret", "_password"))
            )
            if looks_secret and isinstance(value, str) and value.strip():
                raise ConfigError(
                    f"{where}: secrets must not be stored in config; "
                    "use an environment variable or Windows Credential Manager"
                )
            reject_secrets(value, where)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            reject_secrets(value, f"{trail}[{index}]")


def parse_retention(raw: Any) -> Retention:
    mapping = as_mapping(raw or {}, "retention")
    reject_unknown(mapping, {"raw_payload_days", "max_raw_mb"}, "retention")
    days = get_integer(mapping, "raw_payload_days", "retention", default=14)
    max_raw_mb = get_number(mapping, "max_raw_mb", "retention", default=50.0)
    if days < 0:
        raise ConfigError("retention.raw_payload_days must not be negative")
    if max_raw_mb <= 0:
        raise ConfigError("retention.max_raw_mb must be greater than zero")
    return Retention(raw_payload_days=days, max_raw_mb=max_raw_mb)


def validate_encoding(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{where}: expected a non-empty string")
    try:
        codecs.lookup(value)
    except LookupError as exc:
        raise ConfigError(f"{where}: unknown encoding '{value}'") from exc
    return value


def freeze(mapping: Mapping[str, Any]) -> Mapping[str, Any]:
    return MappingProxyType(dict(mapping))
