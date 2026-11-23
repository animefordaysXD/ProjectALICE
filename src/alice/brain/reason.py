"""Tier 2 reasoning for escalated triage batches.

One escalation at a time. Creates a fast-memory task, recalls context, calls
the reason tier, optionally refines with heavy, validates suggestions, queues
them, and marks the escalation done.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Mapping

from ..contracts.memory import MemoryStore, TaskStore
from ..contracts.models import Message, ModelRouter
from ..contracts.outputs import Suggestion, SuggestionSink
from ..core.db import utc_now
from ..core.events import Ledger
from .json_output import OutputFormatError, extract_object
from .prompts import HEAVY_INSTRUCTION, REPAIR_INSTRUCTION, build_reason_messages

logger = logging.getLogger(__name__)

MAX_REASON_TOKENS = 900
MAX_SUGGESTIONS = 3
_CATEGORIES = {"security", "performance", "maintenance", "data", "workflow", "other"}


class ReasoningFormatError(ValueError):
    """The model's reasoning reply was not valid."""


@dataclass(frozen=True)
class Escalation:
    batch_id: int
    event_ids: tuple[int, ...]
    reason: str
    suggestion_text: str | None
    recall_query: str | None
    created: str


@dataclass(frozen=True)
class ReasonSuggestion:
    text: str
    priority: int
    reason: str | None = None
    steps: tuple[str, ...] = ()
    evidence: str | None = None
    category: str | None = None
    confidence: float | None = None


@dataclass(frozen=True)
class ReasoningDecision:
    assessment: str
    suggestions: tuple[ReasonSuggestion, ...] = ()
    needs_heavy: bool = False


@dataclass(frozen=True)
class ReasoningResult:
    events: int
    batch_id: int | None
    suggestions: int
    used_heavy: bool
    attempts: int
    error: str | None = None

    @property
    def ran(self) -> bool:
        return self.events > 0


class ReasoningService:
    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        router: ModelRouter,
        ledger: Ledger,
        memory: MemoryStore,
        tasks: TaskStore,
        sink: SuggestionSink,
        hard_memory: Callable[[], str],
        max_escalations_per_hour: int,
        max_heavy_per_day: int,
        heavy_calls_today: Callable[[], int] = lambda: 0,
    ) -> None:
        self.conn = conn
        self.router = router
        self.ledger = ledger
        self.memory = memory
        self.tasks = tasks
        self.sink = sink
        self.hard_memory = hard_memory
        self.max_escalations_per_hour = max_escalations_per_hour
        self.max_heavy_per_day = max_heavy_per_day
        self.heavy_calls_today = heavy_calls_today

    def run_once(self, now: datetime | None = None) -> ReasoningResult:
        moment = now or datetime.now(timezone.utc)
        stamp = _iso(moment)
        escalation = self._next_escalation(moment)
        if escalation is None:
            return ReasoningResult(0, None, 0, False, 0)

        events = self.ledger.by_ids(list(escalation.event_ids))
        if not events:
            self._record_attempt(escalation.batch_id, done=True)
            return ReasoningResult(0, escalation.batch_id, 0, False, 0, "batch events missing")

        task = self.tasks.create(
            f"Reason about escalation batch {escalation.batch_id}",
            {
                "triage_batch_id": escalation.batch_id,
                "event_ids": list(escalation.event_ids),
            },
        )
        query = escalation.recall_query or events[0].text_redacted[:200]
        recalled = self.memory.recall(query, k=5)
        messages, prefix_len = build_reason_messages(
            events,
            self.hard_memory(),
            task,
            recalled,
            triage_reason=escalation.reason,
            triage_suggestion=escalation.suggestion_text,
        )

        attempts = 0
        decision: ReasoningDecision | None = None
        error: str | None = None
        for attempt in (1, 2):
            attempts = attempt
            completion = self.router.complete(
                "reason",
                messages,
                MAX_REASON_TOKENS,
                purpose="reason",
                cache_prefix_len=prefix_len,
            )
            try:
                decision = parse_reasoning(completion.text)
                break
            except ReasoningFormatError as exc:
                error = str(exc)
                logger.warning(
                    "reasoning reply invalid (attempt=%d batch=%d): %s",
                    attempt,
                    escalation.batch_id,
                    error,
                )
                if attempt == 1:
                    messages = [*messages, Message("user", REPAIR_INSTRUCTION)]

        if decision is None:
            self._record_attempt(escalation.batch_id, done=False)
            self.tasks.complete(task.id, f"reasoning failed: {error}")
            logger.error("reasoning failed for batch %d: %s", escalation.batch_id, error)
            return ReasoningResult(
                events=len(events),
                batch_id=escalation.batch_id,
                suggestions=0,
                used_heavy=False,
                attempts=attempts,
                error=error,
            )

        used_heavy = False
        if (
            decision.needs_heavy
            and self.heavy_calls_today() < self.max_heavy_per_day
        ):
            try:
                heavy = self.router.complete(
                    "heavy",
                    [*messages, Message("user", HEAVY_INSTRUCTION)],
                    MAX_REASON_TOKENS,
                    purpose="reason-heavy",
                    cache_prefix_len=prefix_len,
                )
                decision = parse_reasoning(heavy.text)
                used_heavy = True
            except Exception:
                logger.exception(
                    "heavy refinement failed for batch %d; keeping reason result",
                    escalation.batch_id,
                )

        ids = tuple(event.id for event in events)
        for suggestion in decision.suggestions:
            self.sink.submit(
                Suggestion(
                    text=suggestion.text,
                    priority=suggestion.priority,
                    source_event_ids=ids,
                    reason=suggestion.reason,
                    steps=suggestion.steps,
                    evidence=suggestion.evidence,
                    category=suggestion.category,
                    confidence=suggestion.confidence,
                )
            )
        self.ledger.mark_status(list(ids), "done")
        self._record_attempt(escalation.batch_id, done=True)
        self.tasks.complete(
            task.id, f"{len(decision.suggestions)} suggestions queued"
        )
        logger.info(
            "reasoning batch=%d events=%d suggestions=%d heavy=%s",
            escalation.batch_id,
            len(events),
            len(decision.suggestions),
            used_heavy,
        )
        return ReasoningResult(
            events=len(events),
            batch_id=escalation.batch_id,
            suggestions=len(decision.suggestions),
            used_heavy=used_heavy,
            attempts=attempts,
        )

    def _next_escalation(self, moment: datetime) -> Escalation | None:
        hour_ago = _iso(moment - timedelta(hours=1))
        recent = self.conn.execute(
            "SELECT COUNT(*) FROM triage_batches WHERE reasoned_at >= ?",
            (hour_ago,),
        ).fetchone()[0]
        if int(recent) >= self.max_escalations_per_hour:
            logger.info("reasoning hourly limit reached")
            return None

        row = self.conn.execute(
            """
            SELECT * FROM triage_batches
            WHERE decision = 'escalate' AND reasoned_at IS NULL
            ORDER BY created, id
            LIMIT 1
            """
        ).fetchone()
        if row is None:
            return None
        try:
            event_ids = tuple(int(value) for value in json.loads(row["event_ids"]))
        except (TypeError, ValueError, json.JSONDecodeError):
            event_ids = ()
        return Escalation(
            batch_id=int(row["id"]),
            event_ids=event_ids,
            reason=str(row["reason"]),
            suggestion_text=row["suggestion_text"],
            recall_query=row["recall_query"],
            created=str(row["created"]),
        )

    def _record_attempt(self, batch_id: int, *, done: bool) -> None:
        if done:
            self.conn.execute(
                """
                UPDATE triage_batches
                SET reasoned_at = ?, reason_attempts = reason_attempts + 1
                WHERE id = ?
                """,
                (utc_now(), batch_id),
            )
        else:
            self.conn.execute(
                "UPDATE triage_batches SET reason_attempts = reason_attempts + 1 WHERE id = ?",
                (batch_id,),
            )
        self.conn.commit()


def parse_reasoning(text: str) -> ReasoningDecision:
    try:
        payload = extract_object(text)
    except OutputFormatError as exc:
        raise ReasoningFormatError(str(exc)) from exc

    assessment = payload.get("assessment")
    if not isinstance(assessment, str) or not assessment.strip():
        raise ReasoningFormatError("assessment must be a non-empty string")
    if len(assessment) > 500:
        raise ReasoningFormatError("assessment is too long")

    needs_heavy = payload.get("needs_heavy", False)
    if not isinstance(needs_heavy, bool):
        raise ReasoningFormatError("needs_heavy must be true or false")

    raw_suggestions = payload.get("suggestions", [])
    if not isinstance(raw_suggestions, list):
        raise ReasoningFormatError("suggestions must be a list")
    if len(raw_suggestions) > MAX_SUGGESTIONS:
        raise ReasoningFormatError(f"at most {MAX_SUGGESTIONS} suggestions are allowed")

    suggestions: list[ReasonSuggestion] = []
    for index, item in enumerate(raw_suggestions):
        where = f"suggestions[{index}]"
        if not isinstance(item, dict):
            raise ReasoningFormatError(f"{where} must be an object")
        text = item.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ReasoningFormatError(f"{where}.text must be a non-empty string")
        priority = item.get("priority")
        if isinstance(priority, bool) or not isinstance(priority, int):
            if isinstance(priority, str) and priority.strip().isdigit():
                priority = int(priority.strip())
        if not isinstance(priority, int) or not 1 <= priority <= 3:
            raise ReasoningFormatError(f"{where}.priority must be 1, 2, or 3")
        reason = item.get("reason")
        if reason is not None and not isinstance(reason, str):
            raise ReasoningFormatError(f"{where}.reason must be a string or null")
        if isinstance(reason, str) and len(reason.split()) > 30:
            raise ReasoningFormatError(f"{where}.reason exceeds 30 words")

        raw_steps = item.get("steps", [])
        if raw_steps is None:
            raw_steps = []
        if not isinstance(raw_steps, list):
            raise ReasoningFormatError(f"{where}.steps must be a list")
        if len(raw_steps) > 6:
            raise ReasoningFormatError(f"{where}.steps allows at most 6 items")
        steps: list[str] = []
        for step_index, step in enumerate(raw_steps):
            if not isinstance(step, str) or not step.strip():
                raise ReasoningFormatError(
                    f"{where}.steps[{step_index}] must be a non-empty string"
                )
            if len(step) > 220:
                raise ReasoningFormatError(
                    f"{where}.steps[{step_index}] is too long"
                )
            steps.append(step.strip())

        evidence = item.get("evidence")
        if evidence is not None and not isinstance(evidence, str):
            raise ReasoningFormatError(f"{where}.evidence must be a string or null")
        if isinstance(evidence, str) and len(evidence) > 400:
            raise ReasoningFormatError(f"{where}.evidence is too long")

        category = item.get("category")
        if category is not None and not isinstance(category, str):
            raise ReasoningFormatError(f"{where}.category must be a string or null")
        if isinstance(category, str):
            category = category.strip().casefold() or None
            if category is not None and category not in _CATEGORIES:
                category = "other"

        confidence = item.get("confidence")
        if isinstance(confidence, str):
            try:
                confidence = float(confidence)
            except ValueError:
                raise ReasoningFormatError(
                    f"{where}.confidence must be a number between 0 and 1"
                ) from None
        if confidence is not None:
            if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
                raise ReasoningFormatError(
                    f"{where}.confidence must be a number between 0 and 1"
                )
            confidence = float(confidence)
            if not 0.0 <= confidence <= 1.0:
                raise ReasoningFormatError(
                    f"{where}.confidence must be between 0 and 1"
                )

        suggestions.append(
            ReasonSuggestion(
                text=text.strip(),
                priority=priority,
                reason=(reason.strip() if isinstance(reason, str) and reason.strip() else None),
                steps=tuple(steps),
                evidence=(
                    evidence.strip()
                    if isinstance(evidence, str) and evidence.strip()
                    else None
                ),
                category=category,
                confidence=confidence,
            )
        )

    return ReasoningDecision(
        assessment=assessment.strip(),
        suggestions=tuple(suggestions),
        needs_heavy=needs_heavy,
    )


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
