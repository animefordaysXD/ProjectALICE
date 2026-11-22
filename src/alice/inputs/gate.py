"""Dedupe, salience, and ingestion into the event ledger.

Every observation passes redaction first. The redacted text is normalized so
values that change on every line (timestamps, ids, counters) do not defeat
dedupe, then hashed. Salience is a cheap rule score, not a model judgement:
Tier 1 still decides what matters.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Mapping

from .config import GateConfig, RedactionConfig
from ..core.events import Ledger, LedgerWrite, RawEvent, RawStore
from .redact import Redactor

# Volatile parts of a line. Order matters: structured values first.
_UUID = re.compile(
    r"(?i)\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b"
)
_ISO_TS = re.compile(
    r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?\b"
)
_CLOCK = re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?\b")
_HEX = re.compile(r"(?i)\b(?:0x)?[0-9a-f]{6,}\b")
_NUMBER = re.compile(r"\d+")
_WHITESPACE = re.compile(r"\s+")


@dataclass(frozen=True)
class Salience:
    score: float
    matched_rules: tuple[str, ...]
    novel: bool


@dataclass(frozen=True)
class IngestResult:
    raw: RawEvent
    text_redacted: str
    redactions: Mapping[str, int]
    content_hash: str
    salience: float
    matched_rules: tuple[str, ...]
    novel: bool
    passes: bool
    write: LedgerWrite
    raw_ref: str | None


def normalize(text: str) -> str:
    """Collapse values that vary between otherwise identical lines."""
    text = _UUID.sub("<id>", text)
    text = _ISO_TS.sub("<ts>", text)
    text = _CLOCK.sub("<time>", text)
    text = _HEX.sub("<hex>", text)
    text = _NUMBER.sub("#", text)
    return _WHITESPACE.sub(" ", text).strip()


def content_hash(normalized_text: str) -> str:
    return hashlib.sha256(normalized_text.encode("utf-8")).hexdigest()


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Gate:
    """Turns raw observations into ledger events."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        gate: GateConfig,
        redaction: RedactionConfig,
        raw_store: RawStore,
        redactor: Redactor | None = None,
    ) -> None:
        self.conn = conn
        self.config = gate
        self.redactor = redactor or Redactor(redaction.patterns)
        self.raw_store = raw_store
        self.ledger = Ledger(conn)

    def ingest(self, raw: RawEvent, *, now: datetime | None = None) -> IngestResult | None:
        """Redact, dedupe, score, and write one observation.

        Returns None for blank observations. Raw text is only persisted when
        redaction changed it; otherwise the ledger text is already raw.
        """
        if not raw.text.strip():
            return None

        moment = now or datetime.now(timezone.utc)
        ts = _iso(moment)
        redacted = self.redactor.redact(raw.text)
        normalized = normalize(redacted.text)
        digest = content_hash(normalized)

        previous = self.ledger.last_seen(digest)
        novel = previous is None or previous < self._window_start(moment)
        salience = self.evaluate(
            redacted.text, source_type=raw.source_type, novel=novel
        )

        raw_ref = None
        if redacted.changed:
            raw_ref = self.raw_store.save(raw.text)

        write = self.ledger.record(
            ts=raw.ts or ts,
            source=raw.source,
            kind=raw.kind,
            content_hash=digest,
            text_redacted=redacted.text,
            raw_ref=raw_ref,
            salience=salience.score,
            last_seen=ts,
        )
        return IngestResult(
            raw=raw,
            text_redacted=redacted.text,
            redactions=redacted.counts,
            content_hash=digest,
            salience=salience.score,
            matched_rules=salience.matched_rules,
            novel=novel,
            passes=salience.score >= self.config.min_salience,
            write=write,
            raw_ref=raw_ref,
        )

    def ingest_many(
        self, raw_events: list[RawEvent], *, now: datetime | None = None
    ) -> list[IngestResult]:
        results: list[IngestResult] = []
        for raw in raw_events:
            result = self.ingest(raw, now=now)
            if result is not None:
                results.append(result)
        return results

    def evaluate(self, text: str, *, source_type: str, novel: bool) -> Salience:
        """Rule-based salience in [0, 1]; deterministic and model-free."""
        matched: list[str] = []
        weight = 0.0
        for rule in self.config.rules:
            if rule.pattern.search(text):
                matched.append(rule.name)
                weight += rule.weight

        score = (0.1 + min(0.8, weight)) * self.config.source_weights.get(
            source_type, 1.0
        )
        if novel:
            score += 0.1
        return Salience(
            score=round(min(1.0, score), 3),
            matched_rules=tuple(matched),
            novel=novel,
        )

    def _window_start(self, moment: datetime) -> str:
        window = timedelta(hours=self.config.novelty_window_hours)
        return _iso(moment - window)
