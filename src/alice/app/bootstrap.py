"""Composition root helpers: the only place modules are wired together."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from ..brain.triage import TriageService
from ..core.events import CursorStore, Ledger, RawStore
from ..inputs import Sensor, build_sensors
from ..inputs.gate import Gate
from ..inputs.redact import Redactor
from ..memory.embeddings import build_embedder
from ..memory.service import MemoryService
from ..models import BudgetGuard, Router, UsageLog, build_router
from ..models.fake import FakeProvider
from .settings import AppSettings

DRY_RUN_DECISION = json.dumps(
    {
        "decision": "log",
        "priority": 3,
        "reason": "dry run decision",
        "suggestion_text": None,
        "recall_query": None,
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

    fake = FakeProvider(default_response=DRY_RUN_DECISION)
    providers = {spec.provider: fake for spec in settings.models.tiers.values()}
    return Router(
        models=settings.models.tiers,
        providers=providers,
        usage=UsageLog(conn),
        budget=BudgetGuard(conn, settings.models.budget, settings.data_dir),
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


def build_memory(settings: AppSettings, conn: sqlite3.Connection) -> MemoryService:
    embedder = build_embedder(settings.memory, settings.data_dir / "cache")
    redactor = Redactor(settings.inputs.redaction.patterns)
    return MemoryService(
        conn,
        settings.memory,
        embedder=embedder,
        sanitize=lambda text: redactor.redact(text).text,
    )
