"""Tier 1 triage: batch new events, ask once, validate, apply.

One model call per batch. No events above the gate threshold means no call at
all. On a malformed reply the call is retried once; if it still fails the
events stay `new` for the next run.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from typing import Callable, Sequence

from ..contracts.models import Completion, Message, ModelRouter
from ..core.db import utc_now
from ..core.events import Event, Ledger
from ..core.tokens import estimate_tokens
from .config import Triage
from .prompts import REPAIR_INSTRUCTION, build_triage_messages

logger = logging.getLogger(__name__)

DECISIONS = ("ignore", "log", "suggest", "escalate")
MAX_OUTPUT_TOKENS = 400


class TriageFormatError(ValueError):
    """The model's reply was not a valid triage decision."""


@dataclass(frozen=True)
class TriageDecision:
    decision: str
    priority: int
    reason: str
    suggestion_text: str | None = None
    recall_query: str | None = None


@dataclass(frozen=True)
class TriageResult:
    events: int
    decision: TriageDecision | None
    batch_id: int | None
    attempts: int
    error: str | None = None

    @property
    def ran(self) -> bool:
        return self.events > 0


class TriageService:
    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        router: ModelRouter,
        ledger: Ledger,
        hard_memory: Callable[[], str],
        config: Triage,
        min_salience: float,
    ) -> None:
        self.conn = conn
        self.router = router
        self.ledger = ledger
        self.hard_memory = hard_memory
        self.config = config
        self.min_salience = min_salience

    def run_once(self, now: str | None = None) -> TriageResult:
        events = self._select_batch()
        if not events:
            return TriageResult(events=0, decision=None, batch_id=None, attempts=0)

        messages, prefix_len = build_triage_messages(events, self.hard_memory())
        attempts = 0
        completion: Completion | None = None
        decision: TriageDecision | None = None
        error: str | None = None

        for attempt in (1, 2):
            attempts = attempt
            completion = self.router.complete(
                "triage",
                messages,
                MAX_OUTPUT_TOKENS,
                purpose="triage",
                cache_prefix_len=prefix_len,
            )
            try:
                decision = parse_decision(completion.text)
                break
            except TriageFormatError as exc:
                error = str(exc)
                logger.warning(
                    "triage reply invalid (attempt=%d events=%d): %s",
                    attempt,
                    len(events),
                    error,
                )
                if attempt == 1:
                    messages = [*messages, Message("user", REPAIR_INSTRUCTION)]

        if decision is None:
            logger.error(
                "triage failed after %d attempts (%d events): %s",
                attempts,
                len(events),
                error,
            )
            return TriageResult(
                events=len(events),
                decision=None,
                batch_id=None,
                attempts=attempts,
                error=error,
            )

        status = "escalated" if decision.decision == "escalate" else "triaged"
        event_ids = [event.id for event in events]
        self.ledger.mark_status(event_ids, status)
        batch_id = self._record(
            event_ids,
            decision,
            attempts=attempts,
            completion=completion,
            created=now or utc_now(),
        )
        logger.info(
            "triage batch=%d events=%d decision=%s priority=%d",
            batch_id,
            len(events),
            decision.decision,
            decision.priority,
        )
        return TriageResult(
            events=len(events),
            decision=decision,
            batch_id=batch_id,
            attempts=attempts,
        )

    def _select_batch(self) -> list[Event]:
        selected: list[Event] = []
        used = 0
        for event in self.ledger.pending(self.min_salience, limit=1000):
            cost = estimate_tokens(event.text_redacted) + 16
            if selected and used + cost > self.config.max_batch_tokens:
                break
            selected.append(event)
            used += cost
        return selected

    def _record(
        self,
        event_ids: Sequence[int],
        decision: TriageDecision,
        *,
        attempts: int,
        completion: Completion,
        created: str,
    ) -> int:
        cursor = self.conn.execute(
            """
            INSERT INTO triage_batches
              (created, event_ids, decision, priority, reason, suggestion_text,
               recall_query, attempts, input_tokens, output_tokens, cost_usd)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                created,
                json.dumps(list(event_ids)),
                decision.decision,
                decision.priority,
                decision.reason,
                decision.suggestion_text,
                decision.recall_query,
                attempts,
                completion.input_tokens,
                completion.output_tokens,
                completion.cost_usd,
            ),
        )
        self.conn.commit()
        return int(cursor.lastrowid)


def parse_decision(text: str) -> TriageDecision:
    payload = _extract_object(text)
    raw_decision = payload.get("decision")
    if not isinstance(raw_decision, str):
        raise TriageFormatError("decision must be a string")
    decision = raw_decision.strip().lower()
    if decision not in DECISIONS:
        raise TriageFormatError(f"unknown decision: {raw_decision!r}")

    priority = payload.get("priority")
    if isinstance(priority, bool):
        raise TriageFormatError("priority must be 1, 2, or 3")
    if isinstance(priority, str) and priority.strip().isdigit():
        priority = int(priority.strip())
    if not isinstance(priority, int) or not 1 <= priority <= 3:
        raise TriageFormatError("priority must be 1, 2, or 3")

    reason = payload.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise TriageFormatError("reason must be a non-empty string")
    reason = reason.strip()
    if len(reason.split()) > 20:
        raise TriageFormatError("reason exceeds 20 words")

    suggestion = _optional_text(payload.get("suggestion_text"), "suggestion_text")
    recall = _optional_text(payload.get("recall_query"), "recall_query")
    return TriageDecision(
        decision=decision,
        priority=priority,
        reason=reason,
        suggestion_text=suggestion,
        recall_query=recall,
    )


def _optional_text(value: object, key: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TriageFormatError(f"{key} must be a string or null")
    value = value.strip()
    return value or None


def _extract_object(text: str) -> dict:
    raw = text.strip()
    if raw.startswith("```"):
        parts = raw.split("\n", 1)
        raw = parts[1] if len(parts) == 2 else ""
        raw = raw.rstrip().rstrip("`").strip()
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end <= start:
        raise TriageFormatError("reply did not contain a JSON object")
    try:
        payload = json.loads(raw[start : end + 1])
    except json.JSONDecodeError as exc:
        raise TriageFormatError(f"invalid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise TriageFormatError("reply must be a JSON object")
    return payload
