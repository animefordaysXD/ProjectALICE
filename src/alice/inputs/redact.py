"""Redaction. Runs before anything reaches the ledger or a model.

Built-in masks cover Korean resident registration numbers, phone numbers,
card and bank account numbers, and email addresses. `config.alice.yaml` can
add patterns; each becomes `[NAME]`. Raw values never appear in the result.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Mapping, Sequence

from .config import SecretPattern

_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_RRN = re.compile(r"(?<!\d)\d{6}[ -]?[1-8]\d{6}(?!\d)")
_AMEX = re.compile(r"(?<!\d)3[47]\d{2}[ -]?\d{6}[ -]?\d{5}(?!\d)")
_CARD = re.compile(r"(?<!\d)(?:\d{4}[ -]?){3}\d{4}(?!\d)")
_PHONE_INTL = re.compile(r"(?<!\d)\+82[ -]?\d{1,2}[ -]?\d{3,4}[ -]?\d{4}(?!\d)")
_PHONE_MOBILE = re.compile(r"(?<!\d)01[016789][ -]?\d{3,4}[ -]?\d{4}(?!\d)")
_PHONE_LANDLINE = re.compile(r"(?<!\d)0\d{1,2}[ -]\d{3,4}[ -]\d{4}(?!\d)")
_ACCOUNT = re.compile(r"(?<![\d\-])(?:\d{2,6}[ -]){2,3}\d{2,8}(?![\d\-])")

_ACCOUNT_SPLIT = re.compile(r"[ -]")

BUILTIN_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("EMAIL", _EMAIL),
    ("RRN", _RRN),
    ("AMEX", _AMEX),
    ("CARD", _CARD),
    ("PHONE", _PHONE_INTL),
    ("PHONE", _PHONE_MOBILE),
    ("PHONE", _PHONE_LANDLINE),
    ("ACCOUNT", _ACCOUNT),
)


@dataclass(frozen=True)
class RedactionResult:
    text: str
    counts: Mapping[str, int] = field(default_factory=dict)

    @property
    def changed(self) -> bool:
        return bool(self.counts)

    @property
    def total(self) -> int:
        return sum(self.counts.values())


class Redactor:
    """Applies built-in masks, then any user-configured patterns."""

    def __init__(self, patterns: Sequence[SecretPattern] = ()) -> None:
        self._steps = list(BUILTIN_PATTERNS) + [
            (pattern.name, pattern.pattern) for pattern in patterns
        ]

    def redact(self, text: str) -> RedactionResult:
        counts: dict[str, int] = {}
        masked = text
        for name, pattern in self._steps:
            if name == "ACCOUNT":
                masked = pattern.sub(lambda match: _mask_account(match, counts), masked)
            else:
                masked = pattern.sub(
                    lambda match, name=name: _mask(name, counts), masked
                )
        return RedactionResult(text=masked, counts=MappingProxyType(dict(counts)))


def _mask(name: str, counts: dict[str, int]) -> str:
    counts[name] = counts.get(name, 0) + 1
    return f"[{name}]"


def _mask_account(match: re.Match[str], counts: dict[str, int]) -> str:
    """Mask account-like numbers, but leave dates and times alone."""
    parts = [part for part in _ACCOUNT_SPLIT.split(match.group(0)) if part]
    digits = sum(len(part) for part in parts)
    if digits < 10 or not parts or len(parts[-1]) < 4:
        return match.group(0)
    counts["ACCOUNT"] = counts.get("ACCOUNT", 0) + 1
    return "[ACCOUNT]"


def redact(text: str, patterns: Sequence[SecretPattern] = ()) -> RedactionResult:
    """Convenience wrapper for one-off redaction."""
    return Redactor(patterns).redact(text)
