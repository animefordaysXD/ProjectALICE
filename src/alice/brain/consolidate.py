"""Nightly consolidation: hourly summaries, a daily summary, proposals, pruning."""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Callable, Sequence

from ..contracts.memory import MemoryStore
from ..contracts.models import Message, ModelRouter
from ..core.config import Retention
from ..core.db import utc_now
from ..core.events import Event, Ledger, RawStore
from ..core.tokens import estimate_tokens
from .config import Consolidation
from .json_output import OutputFormatError, extract_object
from .prompts import (
    REPAIR_INSTRUCTION,
    build_consolidate_messages,
    build_daily_consolidation_messages,
)

logger = logging.getLogger(__name__)

MAX_OUTPUT_TOKENS = 800


class ConsolidationFormatError(ValueError):
    """The model's consolidation reply was not valid."""


@dataclass(frozen=True)
class ProposalDraft:
    key: str
    value: str
    reason: str | None = None


@dataclass(frozen=True)
class ConsolidationResult:
    day: str
    ran: bool
    hours: int
    proposals: int
    raw_deleted: int
    error: str | None = None


@dataclass(frozen=True)
class _Hour:
    start: datetime
    events: list[Event]


class ConsolidationService:
    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        router: ModelRouter,
        ledger: Ledger,
        memory: MemoryStore,
        hard_memory: Callable[[], str],
        raw_store: RawStore | None,
        config: Consolidation,
        retention: Retention,
    ) -> None:
        self.conn = conn
        self.router = router
        self.ledger = ledger
        self.memory = memory
        self.hard_memory = hard_memory
        self.raw_store = raw_store
        self.config = config
        self.retention = retention

    def run(
        self,
        day: date | None = None,
        *,
        now: datetime | None = None,
        force: bool = False,
    ) -> ConsolidationResult:
        moment = now or datetime.now(timezone.utc)
        local = moment.astimezone()
        target = day or (local.date() - timedelta(days=1))
        day_key = target.isoformat()
        if not force and self._already_run(day_key):
            return ConsolidationResult(day_key, False, 0, 0, 0)

        start_local = datetime.combine(target, time.min, tzinfo=local.tzinfo)
        end_local = start_local + timedelta(days=1)
        start_utc = _iso(start_local)
        end_utc = _iso(end_local)

        events = self.ledger.between(start_utc, end_utc, limit=self.config.max_events)
        hours = self._group_hours(events, local.tzinfo)

        hourly: list[tuple[str, str, str]] = []  # (start_iso, label, summary)
        for hour in hours:
            key = _iso(hour.start)
            label = hour.start.strftime("%Y-%m-%d %H:%M")
            if self.memory.summary_exists("hour", key):
                text = self.memory.summary_text("hour", key)
                if text:
                    hourly.append((key, label, text))
                continue
            try:
                summary = self._summarize_hour(hour.events, label)
            except Exception as exc:
                logger.warning("hourly consolidation failed for %s: %s", label, exc)
                continue
            self.memory.record_summary("hour", key, _iso(hour.start + timedelta(hours=1)), summary)
            hourly.append((key, label, summary))

        proposals = 0
        error: str | None = None
        day_start = _iso(start_local)
        if hourly and not self.memory.summary_exists("day", day_start):
            try:
                summary, drafts = self._summarize_day(
                    [(label, text) for _, label, text in hourly], day_key
                )
                self.memory.record_summary("day", day_start, _iso(end_local), summary)
                for draft in drafts:
                    self.memory.propose(draft.key, draft.value, draft.reason or "")
                    proposals += 1
            except Exception as exc:
                error = str(exc)
                logger.error("daily consolidation failed for %s: %s", day_key, error)

        if hourly and error is None:
            self._mark_run(day_key, len(hourly), proposals)
        elif not hours:
            self._mark_run(day_key, 0, 0)
        elif error is not None and not hourly:
            self._mark_run(day_key, 0, 0)

        raw_deleted = self._prune_raw(moment)
        return ConsolidationResult(
            day=day_key,
            ran=True,
            hours=len(hourly),
            proposals=proposals,
            raw_deleted=raw_deleted,
            error=error,
        )

    def _summarize_hour(self, events: Sequence[Event], label: str) -> str:
        selected = _limit_events(events, self.config.max_tokens)
        messages, prefix = build_consolidate_messages(selected, self.hard_memory(), label)
        text = self._call("consolidate-hour", messages, prefix)
        return parse_hourly_summary(text)

    def _summarize_day(
        self, hourly: Sequence[tuple[str, str]], day_key: str
    ) -> tuple[str, tuple[ProposalDraft, ...]]:
        messages, prefix = build_daily_consolidation_messages(
            hourly, self.hard_memory(), day_key
        )
        text = self._call("consolidate-day", messages, prefix)
        return parse_daily_summary(text)

    def _call(self, purpose: str, messages: list[Message], prefix: int) -> str:
        last_error = ""
        for attempt in (1, 2):
            completion = self.router.complete(
                "consolidate",
                messages,
                MAX_OUTPUT_TOKENS,
                purpose=purpose,
                cache_prefix_len=prefix,
            )
            try:
                if purpose == "consolidate-hour":
                    parse_hourly_summary(completion.text)
                else:
                    parse_daily_summary(completion.text)
                return completion.text
            except ConsolidationFormatError as exc:
                last_error = str(exc)
                logger.warning(
                    "consolidation reply invalid (%s attempt=%d): %s",
                    purpose,
                    attempt,
                    last_error,
                )
                if attempt == 1:
                    messages = [*messages, Message("user", REPAIR_INSTRUCTION)]
        raise ConsolidationFormatError(last_error or "invalid consolidation reply")

    def _group_hours(self, events: Sequence[Event], tz) -> list[_Hour]:
        grouped: dict[datetime, list[Event]] = {}
        for event in events:
            moment = _parse_ts(event.ts).astimezone(tz)
            key = moment.replace(minute=0, second=0, microsecond=0)
            grouped.setdefault(key, []).append(event)
        return [_Hour(start=key, events=grouped[key]) for key in sorted(grouped)]

    def _already_run(self, day_key: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM consolidation_runs WHERE day = ?", (day_key,)
        ).fetchone()
        return row is not None

    def _mark_run(self, day_key: str, summaries: int, proposals: int) -> None:
        self.conn.execute(
            """
            INSERT INTO consolidation_runs
              (day, created, summary_count, proposal_count, raw_deleted)
            VALUES (?, ?, ?, ?, 0)
            ON CONFLICT(day) DO UPDATE SET
              created = excluded.created,
              summary_count = excluded.summary_count,
              proposal_count = excluded.proposal_count
            """,
            (day_key, utc_now(), summaries, proposals),
        )
        self.conn.commit()

    def _prune_raw(self, moment: datetime) -> int:
        if self.raw_store is None:
            return 0
        cutoff_dt = moment.astimezone(timezone.utc) - timedelta(
            days=self.retention.raw_payload_days
        )
        cutoff = _iso(cutoff_dt)
        cutoff_epoch = cutoff_dt.timestamp()
        keep = {
            str(row[0])
            for row in self.conn.execute(
                "SELECT DISTINCT raw_ref FROM events WHERE ts >= ? AND raw_ref IS NOT NULL",
                (cutoff,),
            ).fetchall()
        }
        deleted = self.raw_store.prune(cutoff_epoch, keep)
        self.conn.execute(
            "UPDATE events SET raw_ref = NULL WHERE ts < ? AND raw_ref IS NOT NULL",
            (cutoff,),
        )
        self.conn.commit()
        return deleted


def parse_hourly_summary(text: str) -> str:
    try:
        payload = extract_object(text)
    except OutputFormatError as exc:
        raise ConsolidationFormatError(str(exc)) from exc
    summary = payload.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        raise ConsolidationFormatError("summary must be a non-empty string")
    if len(summary) > 2000:
        raise ConsolidationFormatError("summary is too long")
    return summary.strip()


def parse_daily_summary(text: str) -> tuple[str, tuple[ProposalDraft, ...]]:
    try:
        payload = extract_object(text)
    except OutputFormatError as exc:
        raise ConsolidationFormatError(str(exc)) from exc
    summary = payload.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        raise ConsolidationFormatError("summary must be a non-empty string")
    raw = payload.get("proposals", [])
    if not isinstance(raw, list):
        raise ConsolidationFormatError("proposals must be a list")
    if len(raw) > 3:
        raise ConsolidationFormatError("at most 3 proposals are allowed")
    drafts: list[ProposalDraft] = []
    for index, item in enumerate(raw):
        where = f"proposals[{index}]"
        if not isinstance(item, dict):
            raise ConsolidationFormatError(f"{where} must be an object")
        key = item.get("key")
        value = item.get("value")
        reason = item.get("reason")
        if not isinstance(key, str) or not key.strip():
            raise ConsolidationFormatError(f"{where}.key must be a non-empty string")
        if not isinstance(value, str) or not value.strip():
            raise ConsolidationFormatError(f"{where}.value must be a non-empty string")
        if reason is not None and not isinstance(reason, str):
            raise ConsolidationFormatError(f"{where}.reason must be a string or null")
        drafts.append(
            ProposalDraft(
                key=key.strip(),
                value=value.strip(),
                reason=(reason.strip() if isinstance(reason, str) and reason.strip() else None),
            )
        )
    return summary.strip(), tuple(drafts)


def _limit_events(events: Sequence[Event], max_tokens: int) -> list[Event]:
    selected: list[Event] = []
    used = 0
    for event in events:
        cost = estimate_tokens(event.text_redacted) + 16
        if selected and used + cost > max_tokens:
            break
        selected.append(event)
        used += cost
    return selected


def _parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
