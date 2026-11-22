"""Composition root helpers: the only place modules are wired together."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from ..brain.reason import ReasoningService
from ..brain.triage import TriageService
from ..contracts.outputs import BudgetSnapshot
from ..core.events import CursorStore, Ledger, RawStore
from ..inputs import Sensor, build_sensors
from ..inputs.gate import Gate
from ..inputs.redact import Redactor
from ..memory.embeddings import build_embedder
from ..memory.service import MemoryService
from ..models import BudgetGuard, Router, UsageLog, build_router
from ..models.budget import day_start
from ..models.fake import FakeProvider
from ..outputs import DigestWriter, SuggestionQueue, build_notifier
from .settings import AppSettings

DRY_RUN_TRIAGE = json.dumps(
    {
        "decision": "escalate",
        "priority": 1,
        "reason": "dry run escalation",
        "suggestion_text": "Review the escalated event.",
        "recall_query": None,
    }
)
DRY_RUN_REASON = json.dumps(
    {
        "assessment": "Dry run escalation for the fixture.",
        "suggestions": [
            {
                "text": "Dry run suggestion for the escalated batch.",
                "priority": 2,
                "reason": "dry run",
            }
        ],
        "needs_heavy": False,
    }
)


@dataclass(frozen=True)
class Ingestion:
    sensors: list[Sensor]
    gate: Gate


def build_ingestion(settings: AppSettings, conn: sqlite3.Connection) -> Ingestion:
    """Sensor fan-out plus the gate that writes the core ledger."""
    cursors = CursorStore(conn)
    return Ingestion(
        sensors=build_sensors(
            settings.inputs.sources, root=settings.root, cursors=cursors
        ),
        gate=Gate(
            conn,
            gate=settings.inputs.gate,
            redaction=settings.inputs.redaction,
            raw_store=RawStore(settings.raw_dir),
        ),
    )


def build_models(
    settings: AppSettings, conn: sqlite3.Connection, *, dry_run: bool = False
) -> Router:
    if not dry_run:
        return build_router(settings.models, data_dir=settings.data_dir, conn=conn)

    responses: dict[str, str] = {}
    for tier, spec in settings.models.tiers.items():
        responses[spec.model] = (
            DRY_RUN_REASON if tier in ("reason", "heavy") else DRY_RUN_TRIAGE
        )
    fake = FakeProvider(
        default_response=DRY_RUN_TRIAGE,
        responses_by_model=responses,
    )
    providers = {spec.provider: fake for spec in settings.models.tiers.values()}
    return Router(
        models=settings.models.tiers,
        providers=providers,
        usage=UsageLog(conn),
        budget=BudgetGuard(conn, settings.models.budget, settings.data_dir),
    )


def build_memory(settings: AppSettings, conn: sqlite3.Connection) -> MemoryService:
    embedder = build_embedder(settings.memory, settings.data_dir / "cache")
    redactor = Redactor(settings.inputs.redaction.patterns)
    return MemoryService(
        conn,
        settings.memory,
        embedder=embedder,
        sanitize=lambda text: redactor.redact(text).text,
    )


def build_triage(
    settings: AppSettings,
    conn: sqlite3.Connection,
    router: Router,
    memory: MemoryService,
) -> TriageService:
    return TriageService(
        conn,
        router=router,
        ledger=Ledger(conn),
        hard_memory=memory.hard_block,
        config=settings.brain.triage,
        min_salience=settings.inputs.gate.min_salience,
    )


def build_queue(conn: sqlite3.Connection) -> SuggestionQueue:
    return SuggestionQueue(conn)


def build_reasoning(
    settings: AppSettings,
    conn: sqlite3.Connection,
    router: Router,
    memory: MemoryService,
    queue: SuggestionQueue,
) -> ReasoningService:
    usage = UsageLog(conn)
    return ReasoningService(
        conn,
        router=router,
        ledger=Ledger(conn),
        memory=memory,
        tasks=memory.fast,
        sink=queue,
        hard_memory=memory.hard_block,
        max_escalations_per_hour=settings.models.budget.max_escalations_per_hour,
        max_heavy_per_day=settings.models.budget.max_heavy_per_day,
        heavy_calls_today=lambda: usage.count_since("heavy", day_start()),
    )


def build_digest(
    settings: AppSettings,
    conn: sqlite3.Connection,
    guard: BudgetGuard,
) -> DigestWriter:
    def snapshot() -> BudgetSnapshot:
        status = guard.status()
        paused = status.paused
        return BudgetSnapshot(
            paused=(f"{paused.since} ({paused.reason})" if paused else None),
            calls_today=status.calls_today,
            spend_today=status.spend_today,
            soft_cap=status.soft_cap,
            hard_cap=status.hard_cap,
            alerts=guard.alerts_since(day_start()),
        )

    return DigestWriter(conn, settings.data_dir, settings.outputs.notify, snapshot)


def build_notifications(settings: AppSettings, *, dry_run: bool = False):
    return build_notifier(settings.outputs.notify, dry_run=dry_run)