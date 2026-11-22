"""Input source, gate, and redaction configuration."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Mapping

from ..core.config import (
    ConfigError,
    as_mapping,
    compile_regex,
    freeze,
    get_boolean,
    get_number,
    get_string,
    reject_unknown,
    validate_encoding,
)

SOURCE_TYPES = ("logfile", "message")
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
class InputsConfig:
    sources: tuple[Source, ...]
    gate: GateConfig
    redaction: RedactionConfig


def parse_inputs(sources_raw: Any, gate_raw: Any, redaction_raw: Any) -> InputsConfig:
    return InputsConfig(
        sources=_sources(sources_raw),
        gate=_gate(as_mapping(gate_raw or {}, "gate")),
        redaction=_redaction(as_mapping(redaction_raw or {}, "redaction")),
    )


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
    reject_unknown(options, allowed, where)
