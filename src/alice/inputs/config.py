"""Input source, gate, and redaction configuration."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from ..core.config import (
    ConfigError,
    as_mapping,
    compile_regex,
    freeze,
    get_boolean,
    get_integer,
    get_number,
    get_string,
    reject_unknown,
    resolve_path,
    validate_encoding,
)

SOURCE_TYPES = ("logfile", "message", "logscan")
_PLACEHOLDER_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")

DEFAULT_SALIENCE_RULES: tuple[tuple[str, str, float], ...] = (
    ("error_en", r"(?i)\b(error|failed|failure|exception|critical|panic|fatal)\b", 0.6),
    ("error_ko", r"(실패|오류|에러|장애|긴급|중단)", 0.6),
    ("warn_en", r"(?i)\b(warn|warning|deprecated)\b", 0.3),
    ("warn_ko", r"(경고|주의|지연)", 0.3),
    ("security_en", r"(?i)\b(unauthorized|forbidden|denied|breach|leak)\b", 0.7),
    ("security_ko", r"(권한|거부|유출|침해)", 0.7),
)


@dataclass(frozen=True)
class Source:
    type: str
    options: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SalienceRule:
    name: str
    pattern: re.Pattern[str]
    weight: float


def _default_rules() -> tuple[SalienceRule, ...]:
    return tuple(
        SalienceRule(name=name, pattern=re.compile(pattern), weight=weight)
        for name, pattern, weight in DEFAULT_SALIENCE_RULES
    )


@dataclass(frozen=True)
class GateConfig:
    min_salience: float = 0.5
    novelty_window_hours: float = 24.0
    source_weights: Mapping[str, float] = field(
        default_factory=lambda: freeze({"logfile": 1.0, "message": 1.2})
    )
    rules: tuple[SalienceRule, ...] = field(default_factory=_default_rules)


@dataclass(frozen=True)
class SecretPattern:
    name: str
    pattern: re.Pattern[str]


@dataclass(frozen=True)
class RedactionConfig:
    patterns: tuple[SecretPattern, ...] = ()


@dataclass(frozen=True)
class FilesConfig:
    """Read-only roots Alice may attach to chat."""

    roots: tuple[Path, ...]
    max_bytes: int = 262_144
    max_files: int = 5


@dataclass(frozen=True)
class InputsConfig:
    sources: tuple[Source, ...]
    gate: GateConfig
    redaction: RedactionConfig
    files: FilesConfig


def parse_inputs(
    sources_raw: Any,
    gate_raw: Any,
    redaction_raw: Any,
    files_raw: Any = None,
    *,
    root: Path = Path("."),
) -> InputsConfig:
    return InputsConfig(
        sources=_sources(sources_raw),
        gate=_gate(as_mapping(gate_raw or {}, "gate")),
        redaction=_redaction(as_mapping(redaction_raw or {}, "redaction")),
        files=_files(files_raw, root),
    )


def _files(raw: Any, root: Path) -> FilesConfig:
    mapping = as_mapping(raw or {}, "files")
    reject_unknown(mapping, {"roots", "max_bytes", "max_files"}, "files")

    roots_raw = mapping.get("roots", ["~"])
    if not isinstance(roots_raw, list) or not roots_raw:
        raise ConfigError("files.roots must be a non-empty list of paths")
    roots: list[Path] = []
    for index, value in enumerate(roots_raw):
        if not isinstance(value, str) or not value.strip():
            raise ConfigError(f"files.roots[{index}]: expected a non-empty path string")
        roots.append(resolve_path(Path(value).expanduser(), root))

    max_bytes = get_integer(mapping, "max_bytes", "files", default=262_144)
    if max_bytes <= 0:
        raise ConfigError("files.max_bytes must be greater than zero")
    max_files = get_integer(mapping, "max_files", "files", default=5)
    if not 1 <= max_files <= 20:
        raise ConfigError("files.max_files must be between 1 and 20")
    return FilesConfig(roots=tuple(roots), max_bytes=max_bytes, max_files=max_files)


def _gate(raw: Mapping[str, Any]) -> GateConfig:
    reject_unknown(
        raw, {"min_salience", "novelty_window_hours", "source_weights", "rules"}, "gate"
    )
    default = GateConfig()
    min_salience = get_number(raw, "min_salience", "gate", default=default.min_salience)
    window = get_number(
        raw, "novelty_window_hours", "gate", default=default.novelty_window_hours
    )
    if not 0.0 <= min_salience <= 1.0:
        raise ConfigError("gate.min_salience must be between 0 and 1")
    if window <= 0:
        raise ConfigError("gate.novelty_window_hours must be greater than zero")

    if "source_weights" in raw:
        weights_raw = as_mapping(raw["source_weights"], "gate.source_weights")
        weights: dict[str, float] = {}
        for source_type, weight in weights_raw.items():
            if not isinstance(source_type, str) or not source_type:
                raise ConfigError("gate.source_weights: keys must be source types")
            if isinstance(weight, bool) or not isinstance(weight, (int, float)) or weight < 0:
                raise ConfigError(f"gate.source_weights.{source_type}: expected a number >= 0")
            weights[source_type] = float(weight)
        source_weights: Mapping[str, float] = freeze(weights)
    else:
        source_weights = default.source_weights

    if "rules" not in raw:
        rules = default.rules
    else:
        rules_raw = raw["rules"]
        if not isinstance(rules_raw, list):
            raise ConfigError("gate.rules must be a list")
        parsed: list[SalienceRule] = []
        seen: set[str] = set()
        for index, item in enumerate(rules_raw):
            where = f"gate.rules[{index}]"
            mapping = as_mapping(item, where)
            reject_unknown(mapping, {"name", "pattern", "weight"}, where)
            name = get_string(mapping, "name", where)
            if name in seen:
                raise ConfigError(f"{where}.name: duplicate rule name '{name}'")
            seen.add(name)
            pattern = compile_regex(mapping, "pattern", where)
            weight = get_number(mapping, "weight", where, default=0.0)
            if not 0.0 <= weight <= 1.0:
                raise ConfigError(f"{where}.weight must be between 0 and 1")
            parsed.append(SalienceRule(name=name, pattern=pattern, weight=weight))
        rules = tuple(parsed)

    return GateConfig(
        min_salience=min_salience,
        novelty_window_hours=window,
        source_weights=source_weights,
        rules=rules,
    )


def _redaction(raw: Mapping[str, Any]) -> RedactionConfig:
    reject_unknown(raw, {"patterns"}, "redaction")
    patterns_raw = raw.get("patterns", [])
    if patterns_raw is None:
        return RedactionConfig()
    if not isinstance(patterns_raw, list):
        raise ConfigError("redaction.patterns must be a list")
    patterns: list[SecretPattern] = []
    seen: set[str] = set()
    for index, item in enumerate(patterns_raw):
        where = f"redaction.patterns[{index}]"
        mapping = as_mapping(item, where)
        reject_unknown(mapping, {"name", "pattern"}, where)
        name = get_string(mapping, "name", where).upper()
        if not _PLACEHOLDER_RE.match(name):
            raise ConfigError(f"{where}.name must be letters, digits, and underscores")
        if name in seen:
            raise ConfigError(f"{where}.name: duplicate pattern name '{name}'")
        seen.add(name)
        patterns.append(SecretPattern(name=name, pattern=compile_regex(mapping, "pattern", where)))
    return RedactionConfig(patterns=tuple(patterns))


def _sources(value: Any) -> tuple[Source, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ConfigError("sources must be a list")
    sources: list[Source] = []
    for index, item in enumerate(value):
        where = f"sources[{index}]"
        mapping = as_mapping(item, where)
        source_type = get_string(mapping, "type", where)
        if source_type not in SOURCE_TYPES:
            raise ConfigError(
                f"{where}.type: unsupported source type '{source_type}' "
                f"(supported: {', '.join(SOURCE_TYPES)})"
            )
        options = {key: val for key, val in mapping.items() if key != "type"}
        _validate_source_options(source_type, options, where)
        sources.append(Source(type=source_type, options=freeze(options)))
    return tuple(sources)


def _validate_source_options(source_type: str, options: Mapping[str, Any], where: str) -> None:
    path = options.get("path")
    if not isinstance(path, str) or not path.strip():
        raise ConfigError(f"{where}.path: expected a non-empty path string")
    if "glob" in options and (not isinstance(options["glob"], str) or not options["glob"].strip()):
        raise ConfigError(f"{where}.glob: expected a non-empty string")
    if "encoding" in options:
        validate_encoding(options["encoding"], f"{where}.encoding")
    if "from_start" in options and not isinstance(options["from_start"], bool):
        raise ConfigError(f"{where}.from_start: expected true or false")
    if "max_lines" in options:
        max_lines = options["max_lines"]
        if isinstance(max_lines, bool) or not isinstance(max_lines, int) or max_lines <= 0:
            raise ConfigError(f"{where}.max_lines: expected an integer greater than zero")

    allowed = {"path", "glob", "encoding", "from_start", "max_lines"}
    if source_type == "message":
        allowed = {"path", "adapter", "encoding", "from_start", "max_lines"}
        adapter = options.get("adapter", "stub")
        if adapter != "stub":
            raise ConfigError(
                f"{where}.adapter: '{adapter}' is not built yet (only 'stub' is available)"
            )
    elif source_type == "logscan":
        allowed = {
            "path",
            "glob",
            "globs",
            "encoding",
            "from_start",
            "max_lines",
            "exclude_dirs",
            "exclude_paths",
            "max_files",
            "refresh_minutes",
            "max_depth",
            "recent_days",
            "text_only",
        }
        if "glob" in options and "globs" in options:
            raise ConfigError(f"{where}: use either glob or globs, not both")
        if "text_only" in options and not isinstance(options["text_only"], bool):
            raise ConfigError(f"{where}.text_only: expected true or false")
        _validate_globs(options, where)
        _validate_string_list(options, "exclude_dirs", where)
        _validate_string_list(options, "exclude_paths", where)
        _validate_positive_int(options, "max_files", where)
        _validate_positive_int(options, "refresh_minutes", where)
        _validate_positive_int(options, "max_depth", where)
        if "recent_days" in options:
            recent = options["recent_days"]
            if isinstance(recent, bool) or not isinstance(recent, int) or recent < 0:
                raise ConfigError(
                    f"{where}.recent_days: expected an integer >= 0"
                )
    reject_unknown(options, allowed, where)


def _validate_globs(options: Mapping[str, Any], where: str) -> None:
    if "globs" not in options:
        return
    globs = options["globs"]
    if isinstance(globs, str):
        if not globs.strip():
            raise ConfigError(f"{where}.globs: expected a non-empty string")
        return
    if not isinstance(globs, list) or not globs:
        raise ConfigError(f"{where}.globs: expected a non-empty list of globs")
    for index, pattern in enumerate(globs):
        if not isinstance(pattern, str) or not pattern.strip():
            raise ConfigError(
                f"{where}.globs[{index}]: expected a non-empty string"
            )


def _validate_string_list(
    options: Mapping[str, Any], key: str, where: str
) -> None:
    if key not in options:
        return
    values = options[key]
    if not isinstance(values, list):
        raise ConfigError(f"{where}.{key}: expected a list of strings")
    for index, value in enumerate(values):
        if not isinstance(value, str) or not value.strip():
            raise ConfigError(
                f"{where}.{key}[{index}]: expected a non-empty string"
            )


def _validate_positive_int(
    options: Mapping[str, Any], key: str, where: str
) -> None:
    if key not in options:
        return
    value = options[key]
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ConfigError(
            f"{where}.{key}: expected an integer greater than zero"
        )
