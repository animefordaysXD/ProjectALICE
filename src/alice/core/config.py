"""Shared configuration helpers and core settings.

Modules own their own sections; this file only provides the parser helpers,
`ConfigError`, and the core-owned settings (runtime paths + retention).
"""

from __future__ import annotations

import codecs
import re
import sys
from dataclasses import dataclass, field
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
class Backup:
    enabled: bool = True
    dir: Path = Path("backups")
    hour: int = 4
    keep: int = 7


@dataclass(frozen=True)
class Actions:
    enabled: bool = False
    allowed_programs: tuple[str, ...] = (
        "winget",
        "pip",
        "python",
        "git",
        "opencode-cli",
    )
    timeout_seconds: int = 120
    max_output_chars: int = 20_000


@dataclass(frozen=True)
class CoreConfig:
    data_dir: Path
    seed_dir: Path
    retention: Retention
    backup: Backup = field(default_factory=Backup)
    actions: Actions = field(default_factory=Actions)


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


def parse_backup(raw: Any, data_dir: Path) -> Backup:
    mapping = as_mapping(raw or {}, "backup")
    reject_unknown(mapping, {"enabled", "dir", "hour", "keep"}, "backup")
    enabled = get_boolean(mapping, "enabled", "backup", default=True)
    hour = get_integer(mapping, "hour", "backup", default=4)
    keep = get_integer(mapping, "keep", "backup", default=7)
    if not 0 <= hour <= 23:
        raise ConfigError("backup.hour must be between 0 and 23")
    if keep <= 0:
        raise ConfigError("backup.keep must be greater than zero")
    value = mapping.get("dir", "backups")
    if not isinstance(value, str) or not value.strip():
        raise ConfigError("backup.dir must be a non-empty path string")
    directory = resolve_path(Path(value).expanduser(), data_dir)
    return Backup(enabled=enabled, dir=directory, hour=hour, keep=keep)


def parse_actions(raw: Any) -> Actions:
    mapping = as_mapping(raw or {}, "actions")
    reject_unknown(
        mapping,
        {"enabled", "allowed_programs", "timeout_seconds", "max_output_chars"},
        "actions",
    )
    enabled = get_boolean(mapping, "enabled", "actions", default=False)
    programs_raw = mapping.get(
        "allowed_programs", ["winget", "pip", "python", "git", "opencode-cli"]
    )
    if not isinstance(programs_raw, list) or not programs_raw:
        raise ConfigError("actions.allowed_programs must be a non-empty list")
    programs: list[str] = []
    for index, value in enumerate(programs_raw):
        if not isinstance(value, str) or not value.strip():
            raise ConfigError(
                f"actions.allowed_programs[{index}]: expected a non-empty string"
            )
        name = Path(value.strip()).name.casefold()
        if not name:
            raise ConfigError(f"actions.allowed_programs[{index}]: invalid program")
        programs.append(name)
    timeout = get_integer(mapping, "timeout_seconds", "actions", default=120)
    if not 1 <= timeout <= 3600:
        raise ConfigError("actions.timeout_seconds must be between 1 and 3600")
    output = get_integer(mapping, "max_output_chars", "actions", default=20_000)
    if not 1000 <= output <= 200_000:
        raise ConfigError(
            "actions.max_output_chars must be between 1000 and 200000"
        )
    return Actions(
        enabled=enabled,
        allowed_programs=tuple(programs),
        timeout_seconds=timeout,
        max_output_chars=output,
    )


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
