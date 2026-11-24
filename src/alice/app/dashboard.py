"""Read-only dashboard data for the desktop GUI."""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from ..brain.growth import GrowthMemoryRow, GrowthService, GrowthSessionRow
from ..core.db import current_version, utc_now
from ..core.tokens import estimate_tokens
from ..inputs.redact import Redactor
from ..memory.core_file import CoreMemory
from ..memory.errors import CoreMemoryError, MemoryCapError
from ..models.budget import BudgetGuard, day_start
from .activity import snapshot as activity_snapshot
from .calendar import read_ics
from .settings import AppSettings
from .system_info import snapshot as system_snapshot
from .tools import build_tools


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
    cluster: str | None = None
    duplicate_count: int = 1


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
class UsageSummary:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    cost_usd: float = 0.0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass(frozen=True)
class UsageRow:
    label: str
    calls: int
    input_tokens: int
    output_tokens: int
    cost_usd: float

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass(frozen=True)
class ToolCallRow:
    id: int
    created: str
    tool: str
    ok: bool
    summary: str
    duration_ms: int


@dataclass(frozen=True)
class ToolInfo:
    name: str
    description: str
    read_only: bool
    enabled: bool
    note: str


@dataclass(frozen=True)
class PluginInfo:
    id: str
    version: str
    capabilities: tuple[str, ...]
    description: str
    enabled: bool


@dataclass(frozen=True)
class AmbientAlertRow:
    id: int
    created: str
    kind: str
    title: str
    body: str
    suggestion_id: int | None


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
    core_max_tokens: int
    core_error: str | None
    digest_path: Path
    digest_text: str
    last_consolidation: str | None
    usage_today: UsageSummary
    usage_total: UsageSummary
    usage_by_tier: tuple[UsageRow, ...]
    usage_by_day: tuple[UsageRow, ...]
    tool_calls_today: int
    tool_calls_total: int
    recent_tool_calls: tuple[ToolCallRow, ...]
    tools: tuple[ToolInfo, ...]
    plugins: tuple[PluginInfo, ...]
    hardware: Mapping[str, Any]
    approved_count: int
    active_tasks: int
    embedded_count: int
    summary_count: int
    exploration: Mapping[str, Any] | None
    growth_memories: tuple[GrowthMemoryRow, ...]
    growth_sessions: tuple[GrowthSessionRow, ...]
    growth_stats: Mapping[str, Any]
    activity: Mapping[str, Any] | None
    next_event: str | None
    ambient_alerts: tuple[AmbientAlertRow, ...]


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
            cluster=row["cluster_key"],
            duplicate_count=int(row["duplicate_count"] or 1),
        )
        for row in conn.execute(
            "SELECT id, priority, status, text, reason, steps, evidence, "
            "category, confidence, cluster_key, duplicate_count "
            "FROM suggestions WHERE status != 'merged' "
            "ORDER BY id DESC LIMIT 100"
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

    usage_today = _usage(conn, "WHERE ts >= ?", (day_start(),))
    usage_total = _usage(conn)
    usage_by_tier = tuple(
        UsageRow(
            label=str(row["tier"]),
            calls=int(row["calls"] or 0),
            input_tokens=int(row["input_tokens"] or 0),
            output_tokens=int(row["output_tokens"] or 0),
            cost_usd=float(row["cost_usd"] or 0.0),
        )
        for row in conn.execute(
            "SELECT tier, COUNT(*) AS calls, SUM(input_tokens) AS input_tokens, "
            "SUM(output_tokens) AS output_tokens, SUM(cost_usd) AS cost_usd "
            "FROM usage GROUP BY tier ORDER BY SUM(input_tokens) DESC"
        )
    )
    usage_by_day = tuple(
        UsageRow(
            label=str(row["day"]),
            calls=int(row["calls"] or 0),
            input_tokens=int(row["input_tokens"] or 0),
            output_tokens=int(row["output_tokens"] or 0),
            cost_usd=float(row["cost_usd"] or 0.0),
        )
        for row in conn.execute(
            "SELECT substr(ts, 1, 10) AS day, COUNT(*) AS calls, "
            "SUM(input_tokens) AS input_tokens, SUM(output_tokens) AS output_tokens, "
            "SUM(cost_usd) AS cost_usd FROM usage "
            "GROUP BY day ORDER BY day DESC LIMIT 7"
        )
    )

    tool_calls_today = 0
    tool_calls_total = 0
    recent_tool_calls: tuple[ToolCallRow, ...] = ()
    if _has_table(conn, "tool_calls"):
        tool_calls_today = int(
            conn.execute(
                "SELECT COUNT(*) FROM tool_calls WHERE created >= ?",
                (day_start(),),
            ).fetchone()[0]
        )
        tool_calls_total = int(
            conn.execute("SELECT COUNT(*) FROM tool_calls").fetchone()[0]
        )
        recent_tool_calls = tuple(
            ToolCallRow(
                id=int(row["id"]),
                created=str(row["created"]),
                tool=str(row["tool"]),
                ok=bool(row["ok"]),
                summary=str(row["summary"] or ""),
                duration_ms=int(row["duration_ms"] or 0),
            )
            for row in conn.execute(
                "SELECT id, created, tool, ok, summary, duration_ms "
                "FROM tool_calls ORDER BY id DESC LIMIT 50"
            )
        )

    approved_count = int(
        conn.execute("SELECT COUNT(*) FROM hard_memory WHERE active = 1").fetchone()[0]
    )
    active_tasks = int(
        conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE status = 'open' AND expires >= ?",
            (utc_now(),),
        ).fetchone()[0]
    )
    embedded_count = int(
        conn.execute("SELECT COUNT(*) FROM memory_embeddings").fetchone()[0]
    )
    summary_count = int(
        conn.execute("SELECT COUNT(*) FROM summaries").fetchone()[0]
    )

    plugins = tuple(
        PluginInfo(
            id=str(row["id"]),
            version=str(row["version"]),
            capabilities=tuple(sorted(row["capabilities"] or ())),
            description=str(row["description"] or ""),
            enabled=True,
        )
        for row in conn.execute(
            "SELECT id, version, capabilities, description FROM plugins "
            "ORDER BY id"
        )
    ) if _has_table(conn, "plugins") else ()
    registered = {plugin.id for plugin in plugins}
    plugins = plugins + tuple(
        PluginInfo(
            id=name,
            version="unknown",
            capabilities=(),
            description="Enabled in config but not registered at runtime.",
            enabled=True,
        )
        for name in settings.plugins.enabled
        if name not in registered
    )

    tools = _tools(settings, conn)
    hardware = system_snapshot()
    exploration = _exploration(settings.data_dir)
    growth = GrowthService(conn, None, settings.growth)
    growth_memories = growth.memories(limit=100) if _has_table(conn, "growth_memories") else ()
    growth_sessions = growth.sessions(limit=10) if _has_table(conn, "growth_sessions") else ()
    growth_stats = {
        "memories": len(growth_memories),
        "sessions": len(growth_sessions),
        "top_weight": growth_memories[0].weight if growth_memories else 0.0,
    }

    activity = activity_snapshot()
    activity_data: Mapping[str, Any] | None = None
    if activity is not None:
        redactor = Redactor(settings.inputs.redaction.patterns)
        activity_data = {
            "app": activity.app,
            "category": activity.category,
            "title": redactor.redact(activity.title).text[:200],
            "idle_seconds": activity.idle_seconds,
            "captured_at": activity.captured_at,
        }
    next_event = None
    try:
        calendar_events = read_ics(settings.tools.calendar_file, days=1)
    except (OSError, TypeError):
        calendar_events = []
    if calendar_events:
        event = calendar_events[0]
        next_event = f"{event.summary} at {event.start:%a %H:%M}"

    ambient_alerts: tuple[AmbientAlertRow, ...] = ()
    if _has_table(conn, "ambient_alerts"):
        ambient_alerts = tuple(
            AmbientAlertRow(
                id=int(row["id"]),
                created=str(row["created"]),
                kind=str(row["kind"]),
                title=str(row["title"]),
                body=str(row["body"]),
                suggestion_id=row["suggestion_id"],
            )
            for row in conn.execute(
                "SELECT id, created, kind, title, body, suggestion_id "
                "FROM ambient_alerts ORDER BY id DESC LIMIT 20"
            )
        )

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
        core_max_tokens=settings.memory.hard_max_tokens,
        core_error=core_error,
        digest_path=digest_path,
        digest_text=digest_text,
        last_consolidation=last_consolidation,
        usage_today=usage_today,
        usage_total=usage_total,
        usage_by_tier=usage_by_tier,
        usage_by_day=usage_by_day,
        tool_calls_today=tool_calls_today,
        tool_calls_total=tool_calls_total,
        recent_tool_calls=recent_tool_calls,
        tools=tools,
        plugins=plugins,
        hardware=hardware,
        approved_count=approved_count,
        active_tasks=active_tasks,
        embedded_count=embedded_count,
        summary_count=summary_count,
        exploration=exploration,
        growth_memories=growth_memories,
        growth_sessions=growth_sessions,
        growth_stats=growth_stats,
        activity=activity_data,
        next_event=next_event,
        ambient_alerts=ambient_alerts,
    )


def _usage(
    conn: sqlite3.Connection,
    where: str = "",
    params: tuple[object, ...] = (),
) -> UsageSummary:
    row = conn.execute(
        "SELECT COUNT(*) AS calls, SUM(input_tokens) AS input_tokens, "
        "SUM(output_tokens) AS output_tokens, SUM(cached_tokens) AS cached_tokens, "
        "SUM(cost_usd) AS cost_usd FROM usage " + where,
        params,
    ).fetchone()
    return UsageSummary(
        calls=int(row["calls"] or 0),
        input_tokens=int(row["input_tokens"] or 0),
        output_tokens=int(row["output_tokens"] or 0),
        cached_tokens=int(row["cached_tokens"] or 0),
        cost_usd=float(row["cost_usd"] or 0.0),
    )


def _tools(settings: AppSettings, conn: sqlite3.Connection) -> tuple[ToolInfo, ...]:
    try:
        registry = build_tools(settings, conn)
        specs = registry.specs()
    except Exception:
        return ()
    config = settings.tools
    domains = ", ".join(config.web_domains)
    calendar = config.calendar_file
    email_ready = config.email_enabled and bool(
        os.environ.get(config.email_user_env) and os.environ.get(config.email_password_env)
    )
    rows: list[ToolInfo] = []
    for spec in specs:
        enabled = True
        note = "ready"
        if spec.name == "web.fetch":
            note = (
                f"allowlist: {domains} + public hosts"
                if config.web_allow_public
                else f"allowlist: {domains}"
            )
        elif spec.name == "calendar.upcoming":
            enabled = bool(calendar and calendar.is_file())
            note = str(calendar) if enabled else f"no calendar file at {calendar}"
        elif spec.name == "email.recent":
            enabled = email_ready
            if not config.email_enabled:
                note = "disabled in config"
            elif not email_ready:
                note = (
                    f"set {config.email_user_env} and "
                    f"{config.email_password_env}"
                )
            else:
                note = f"{config.email_host} (read-only IMAP)"
        elif spec.name == "file.read":
            roots = ", ".join(str(root) for root in settings.inputs.files.roots)
            note = f"roots: {roots}"
        rows.append(
            ToolInfo(
                name=spec.name,
                description=spec.description,
                read_only=spec.read_only,
                enabled=enabled,
                note=note,
            )
        )
    return tuple(rows)


def _exploration(data_dir: Path) -> Mapping[str, Any] | None:
    path = data_dir / "exploration" / "latest.json"
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


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
