"""Read-only dashboard data for the desktop GUI."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Mapping

from ..core.db import current_version
from ..core.tokens import estimate_tokens
from ..memory.core_file import CoreMemory
from ..memory.errors import CoreMemoryError, MemoryCapError
from ..models.budget import BudgetGuard
from .settings import AppSettings


@dataclass(frozen=True)
class EventRow:
    id: int
    ts: str
    source: str
    status: str
    salience: float
    text: str


@dataclass(frozen=True)
class SuggestionRow:
    id: int
    priority: int
    status: str
    text: str
    reason: str | None
    steps: tuple[str, ...] = ()
    evidence: str | None = None
    category: str | None = None
    confidence: float | None = None


@dataclass(frozen=True)
class MemoryRow:
    key: str
    value: str
    version: int


@dataclass(frozen=True)
class ProposalRow:
    id: int
    key: str
    value: str
    reason: str | None


@dataclass(frozen=True)
class Dashboard:
    config_path: Path
    data_dir: Path
    schema_version: int
    status_counts: Mapping[str, int]
    new_above_gate: int
    queued_suggestions: int
    pending_proposals: int
    pending_escalations: int
    spend_today: float
    calls_today: int
    soft_cap: float
    hard_cap: float
    paused: str | None
    events: tuple[EventRow, ...]
    suggestions: tuple[SuggestionRow, ...]
    approved: tuple[MemoryRow, ...]
    proposals: tuple[ProposalRow, ...]
    core_path: Path
    core_tokens: int
    core_error: str | None
    digest_path: Path
    digest_text: str
    last_consolidation: str | None


def load_dashboard(settings: AppSettings, conn: sqlite3.Connection) -> Dashboard:
    statuses = {
        str(row["status"]): int(row["n"])
        for row in conn.execute("SELECT status, COUNT(*) AS n FROM events GROUP BY status")
    }
    new_above = int(
        conn.execute(
            "SELECT COUNT(*) FROM events WHERE status = 'new' AND salience >= ?",
            (settings.inputs.gate.min_salience,),
        ).fetchone()[0]
    )
    queued = int(
        conn.execute(
            "SELECT COUNT(*) FROM suggestions WHERE status = 'queued'"
        ).fetchone()[0]
    )
    pending_proposals = int(
        conn.execute(
            "SELECT COUNT(*) FROM proposals WHERE status = 'pending'"
        ).fetchone()[0]
    )
    pending_escalations = 0
    if _has_table(conn, "triage_batches"):
        pending_escalations = int(
            conn.execute(
                "SELECT COUNT(*) FROM triage_batches WHERE decision = 'escalate' "
                "AND reasoned_at IS NULL"
            ).fetchone()[0]
        )

    guard = BudgetGuard(conn, settings.models.budget, settings.data_dir)
    budget = guard.status()
    paused = (
        f"{budget.paused.since} ({budget.paused.reason})" if budget.paused else None
    )

    events = tuple(
        EventRow(
            id=int(row["id"]),
            ts=str(row["ts"]),
            source=str(row["source"]),
            status=str(row["status"]),
            salience=float(row["salience"] or 0.0),
            text=str(row["text_redacted"]),
        )
        for row in conn.execute(
            "SELECT id, ts, source, status, salience, text_redacted "
            "FROM events ORDER BY id DESC LIMIT 100"
        )
    )
    suggestions = tuple(
        SuggestionRow(
            id=int(row["id"]),
            priority=int(row["priority"]),
            status=str(row["status"]),
            text=str(row["text"]),
            reason=row["reason"],
            steps=_steps(row["steps"]),
            evidence=row["evidence"],
            category=row["category"],
            confidence=row["confidence"],
        )
        for row in conn.execute(
            "SELECT id, priority, status, text, reason, steps, evidence, "
            "category, confidence FROM suggestions ORDER BY id DESC LIMIT 100"
        )
    )
    approved = tuple(
        MemoryRow(
            key=str(row["key"]),
            value=str(row["value"]),
            version=int(row["version"]),
        )
        for row in conn.execute(
            "SELECT key, value, version FROM hard_memory WHERE active = 1 ORDER BY key"
        )
    )
    proposals = tuple(
        ProposalRow(
            id=int(row["id"]),
            key=str(row["key"]),
            value=str(row["proposed_value"]),
            reason=row["reason"],
        )
        for row in conn.execute(
            "SELECT id, key, proposed_value, reason FROM proposals "
            "WHERE status = 'pending' ORDER BY id"
        )
    )

    core = CoreMemory(settings.memory.core_memory_file, settings.memory.hard_max_tokens)
    core_tokens = 0
    core_error = None
    try:
        core_tokens = estimate_tokens(core.render())
    except (CoreMemoryError, MemoryCapError) as exc:
        core_error = str(exc)

    today = datetime.now().astimezone().date().isoformat()
    digest_path = settings.data_dir / "digests" / f"{today}.md"
    digest_text = (
        digest_path.read_text(encoding="utf-8") if digest_path.is_file() else ""
    )
    last_consolidation = None
    if _has_table(conn, "consolidation_runs"):
        row = conn.execute(
            "SELECT day FROM consolidation_runs ORDER BY day DESC LIMIT 1"
        ).fetchone()
        if row is not None:
            last_consolidation = str(row[0])

    return Dashboard(
        config_path=settings.path,
        data_dir=settings.data_dir,
        schema_version=current_version(conn),
        status_counts=statuses,
        new_above_gate=new_above,
        queued_suggestions=queued,
        pending_proposals=pending_proposals,
        pending_escalations=pending_escalations,
        spend_today=budget.spend_today,
        calls_today=budget.calls_today,
        soft_cap=budget.soft_cap,
        hard_cap=budget.hard_cap,
        paused=paused,
        events=events,
        suggestions=suggestions,
        approved=approved,
        proposals=proposals,
        core_path=core.path,
        core_tokens=core_tokens,
        core_error=core_error,
        digest_path=digest_path,
        digest_text=digest_text,
        last_consolidation=last_consolidation,
    )


def _steps(raw: object) -> tuple[str, ...]:
    try:
        values = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return ()
    if not isinstance(values, list):
        return ()
    return tuple(str(value) for value in values if str(value).strip())


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone()
    return row is not None
