"""Alice command line: init, run, status, approve, report.

M2 ships `init`, `status`, sensor-only `run`, and `pause`/`resume` for the
model budget kill switch. Triage, approval, and the cost report arrive with
their milestones.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from datetime import datetime
from pathlib import Path

import click

from .. import __version__
from ..brain.triage import TriageResult
from ..core.config import ConfigError, default_config_path
from ..core.db import (
    DatabaseError,
    connect,
    count_rows,
    database_status,
    init_db,
)
from ..core.logging import setup_logging
from ..core.paths import migrate_legacy_state, runtime_mode
from ..inputs import PollStats, poll_once
from ..inputs.config import Source
from ..memory.core_file import CoreMemory
from ..memory.errors import CoreMemoryError, MemoryCapError, ProposalError
from ..memory.seed import SEED_TEMPLATES, ensure_seed_files
from ..core.tokens import estimate_tokens
from ..models.base import ProviderError, ProviderNotConfigured
from ..models import RouterError
from ..models.budget import BudgetError, BudgetGuard, clear_pause, pause_file, set_pause
from .bootstrap import build_ingestion, build_memory, build_models, build_triage
from .settings import AppSettings, load_settings

logger = logging.getLogger(__name__)


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(__version__, prog_name="alice")
@click.option(
    "--config",
    "config_path",
    type=click.Path(path_type=Path, dir_okay=False),
    default=None,
    envvar="ALICE_CONFIG",
    help=f"Path to alice.yaml (default: {default_config_path()}).",
)
@click.option(
    "--data-dir",
    "data_dir",
    type=click.Path(path_type=Path, file_okay=False),
    default=None,
    help="Override the runtime data directory (database, logs, raw, memory).",
)
@click.option("-v", "--verbose", is_flag=True, help="Log pipeline detail to the console.")
@click.pass_context
def main(
    ctx: click.Context,
    config_path: Path | None,
    data_dir: Path | None,
    verbose: bool,
) -> None:
    """Alice: a local, read-only secretary agent. Phase 1 skeleton."""
    ctx.ensure_object(dict)
    ctx.obj["config_path"] = config_path
    ctx.obj["data_dir"] = data_dir
    ctx.obj["verbose"] = verbose


@main.command()
@click.pass_context
def init(ctx: click.Context) -> None:
    """Create the data directory, database, schema, and seed files."""
    config = _load_config(ctx)
    moved = _prepare_runtime(config)
    log_path = setup_logging(config.log_dir, verbose=ctx.obj["verbose"])
    for directory in (config.data_dir, config.log_dir, config.raw_dir):
        directory.mkdir(parents=True, exist_ok=True)

    try:
        result = init_db(config.db_path)
    except DatabaseError as exc:
        raise click.ClickException(str(exc)) from exc

    seeds = ensure_seed_files(config.seed_dir)

    click.echo(_line("config", str(config.path)))
    click.echo(_line("database", str(config.db_path)))
    click.echo(_line("log", str(log_path)))
    click.echo(_line("schema", f"v{result.version}"))
    if moved is not None:
        click.echo(_line("migrated", str(moved)))
    if result.applied:
        names = ", ".join(f"{m.version:03d}_{m.name}" for m in result.applied)
        click.echo(_line("migrations", f"applied {names}"))
    else:
        click.echo(_line("migrations", "none pending"))
    if seeds:
        click.echo(_line("seed files", f"created {', '.join(p.name for p in seeds)}"))
    else:
        click.echo(_line("seed files", f"present in {config.seed_dir}"))
    core = CoreMemory(config.memory.core_memory_file, config.memory.hard_max_tokens)
    core_created = core.ensure()
    click.echo(
        _line(
            "core memory",
            f"{core.path} ({'created' if core_created else 'present'})",
        )
    )


@main.command()
@click.pass_context
def status(ctx: click.Context) -> None:
    """Show config, database, and queue state without changing anything."""
    config = _load_config(ctx)
    _prepare_runtime(config)
    try:
        state = database_status(config.db_path)
    except DatabaseError as exc:
        raise click.ClickException(str(exc)) from exc

    click.echo(_line("alice", __version__))
    click.echo(_line("config", str(config.path)))
    click.echo(_line("repo root", str(config.root)))
    click.echo(_line("mode", runtime_mode()))
    click.echo(_line("data dir", str(config.data_dir)))
    click.echo(_line("database", str(config.db_path)))

    if not state.initialized:
        click.echo(_line("schema", "not initialized (run `alice init`)"))
        return

    schema = f"v{state.version}"
    if state.pending:
        pending = ", ".join(str(version) for version in state.pending)
        schema += f" ({len(state.pending)} pending: {pending}; run `alice init`)"
    else:
        schema += " (up to date)"
    click.echo(_line("schema", schema))
    click.echo(_line("tables", str(len(state.tables))))
    click.echo(_line("sources", _describe_sources(config.sources)))

    conn = connect(config.db_path, create=False)
    try:
        click.echo(
            _line("cursors", f"{_scalar(conn, 'SELECT COUNT(*) FROM cursors', ())} tracked")
        )
        click.echo(_line("events", str(count_rows(conn, "events"))))
        if "triage_batches" in state.tables:
            new_above = _scalar(
                conn,
                "SELECT COUNT(*) FROM events WHERE status = 'new' AND salience >= ?",
                (config.inputs.gate.min_salience,),
            )
            batches = _scalar(conn, "SELECT COUNT(*) FROM triage_batches", ())
            click.echo(_line("triage", f"{new_above} new above gate, {batches} batches"))
        click.echo(
            _line(
                "tasks",
                f"{count_rows(conn, 'tasks')} total, {_scalar(conn, 'SELECT COUNT(*) FROM tasks WHERE status = ?', ('open',))} open",
            )
        )
        click.echo(
            _line(
                "suggestions",
                f"{count_rows(conn, 'suggestions')} total, "
                f"{_scalar(conn, 'SELECT COUNT(*) FROM suggestions WHERE status = ?', ('queued',))} queued",
            )
        )
        click.echo(
            _line(
                "hard memory",
                f"{_scalar(conn, 'SELECT COUNT(DISTINCT key) FROM hard_memory WHERE active = 1', ())} active keys",
            )
        )
        core = CoreMemory(config.memory.core_memory_file, config.memory.hard_max_tokens)
        if core.exists():
            try:
                tokens = estimate_tokens(core.render())
                core_state = f"present (~{tokens} tokens)"
            except (CoreMemoryError, MemoryCapError) as exc:
                core_state = f"error: {exc}"
        else:
            core_state = "missing (run `alice init`)"
        click.echo(_line("core memory", f"{core.path} ({core_state})"))
        click.echo(
            _line(
                "proposals",
                f"{_scalar(conn, 'SELECT COUNT(*) FROM proposals WHERE status = ?', ('pending',))} pending",
            )
        )
        guard = BudgetGuard(conn, config.budget, config.data_dir)
        budget_status = guard.status()
        paused = budget_status.paused
        click.echo(
            _line(
                "paused",
                f"yes since {paused.since} ({paused.reason})" if paused else "no",
            )
        )
        soft_note = " [soft cap reached]" if budget_status.soft_reached else ""
        click.echo(
            _line(
                "spend today",
                f"${budget_status.spend_today:.4f} over {budget_status.calls_today} calls "
                f"(soft ${budget_status.soft_cap:.2f}, hard ${budget_status.hard_cap:.2f})"
                f"{soft_note}",
            )
        )
    finally:
        conn.close()

    missing = [name for name in SEED_TEMPLATES if not (config.seed_dir / name).is_file()]
    if missing:
        click.echo(_line("seed files", f"missing {', '.join(missing)} (run `alice init`)"))
    else:
        click.echo(_line("seed files", f"present in {config.seed_dir}"))


def _load_config(ctx: click.Context) -> AppSettings:
    try:
        return load_settings(
            ctx.obj.get("config_path"), data_dir=ctx.obj.get("data_dir")
        )
    except ConfigError as exc:
        raise click.ClickException(str(exc)) from exc


def _prepare_runtime(config: AppSettings) -> Path | None:
    """Move a legacy `<app>/state` folder before anything opens it."""
    return migrate_legacy_state(config.root, config.data_dir)


@main.command()
@click.option("--reason", default="user", show_default=True, help="Why model calls are paused.")
@click.pass_context
def pause(ctx: click.Context, reason: str) -> None:
    """Block every model call until `alice resume`."""
    config = _load_config(ctx)
    _prepare_runtime(config)
    config.data_dir.mkdir(parents=True, exist_ok=True)
    state = set_pause(config.data_dir, reason)
    click.echo(f"paused since {state.since} ({state.reason}); model calls are blocked")
    click.echo(_line("pause file", str(pause_file(config.data_dir))))


@main.command()
@click.pass_context
def resume(ctx: click.Context) -> None:
    """Allow model calls again."""
    config = _load_config(ctx)
    _prepare_runtime(config)
    if clear_pause(config.data_dir):
        click.echo("resumed; model calls are allowed")
    else:
        click.echo("not paused")


@main.command()
@click.argument("proposal_id", type=int, required=False)
@click.pass_context
def approve(ctx: click.Context, proposal_id: int | None) -> None:
    """Approve a memory proposal, or list pending proposals."""
    config = _load_config(ctx)
    _prepare_runtime(config)
    try:
        state = database_status(config.db_path)
    except DatabaseError as exc:
        raise click.ClickException(str(exc)) from exc
    if not state.initialized:
        raise click.ClickException(
            f"database not initialized at {config.db_path}; run `alice init` first"
        )
    if state.pending:
        raise click.ClickException("database has pending migrations; run `alice init` first")

    conn = connect(config.db_path, create=False)
    try:
        memory = build_memory(config, conn)
        if proposal_id is None:
            pending = memory.pending()
            if not pending:
                click.echo("no pending proposals")
                return
            for proposal in pending:
                reason = f" ({proposal.reason})" if proposal.reason else ""
                click.echo(
                    _line(str(proposal.id), f"{proposal.key} = {proposal.proposed_value}{reason}")
                )
            return

        try:
            entry = memory.approve(proposal_id)
        except (MemoryCapError, ProposalError, CoreMemoryError) as exc:
            raise click.ClickException(str(exc)) from exc
        click.echo(f"approved {entry.key} v{entry.version}: {entry.value}")
    finally:
        conn.close()


@main.command()
@click.option("--once", is_flag=True, help="Poll every source once, ingest, triage, and exit.")
@click.option(
    "--dry-run",
    is_flag=True,
    help="Use the fake model end to end; no network calls and no spend.",
)
@click.option(
    "--interval",
    type=click.FloatRange(min=1.0),
    default=30.0,
    show_default=True,
    help="Seconds between polls in continuous mode.",
)
@click.pass_context
def run(ctx: click.Context, once: bool, dry_run: bool, interval: float) -> None:
    """Poll sensors, write the ledger, and run triage on a timer."""
    config = _load_config(ctx)
    _prepare_runtime(config)
    setup_logging(config.log_dir, verbose=ctx.obj["verbose"])

    try:
        state = database_status(config.db_path)
    except DatabaseError as exc:
        raise click.ClickException(str(exc)) from exc
    if not state.initialized:
        raise click.ClickException(
            f"database not initialized at {config.db_path}; run `alice init` first"
        )
    if state.pending:
        raise click.ClickException("database has pending migrations; run `alice init` first")

    conn = connect(config.db_path, create=False)
    ingestion = build_ingestion(config, conn)
    sensors, gate = ingestion.sensors, ingestion.gate
    memory = build_memory(config, conn)
    try:
        router = build_models(config, conn, dry_run=dry_run)
        triage = build_triage(config, conn, router, memory)
    except (ProviderNotConfigured, RouterError) as exc:
        conn.close()
        raise click.ClickException(str(exc)) from exc
    if not sensors:
        conn.close()
        raise click.ClickException(
            "no sources configured; add one under `sources:` in config/alice.yaml"
        )

    stats: PollStats | None = None
    last_triage = 0.0
    triage_interval = max(1.0, config.brain.triage.interval_minutes * 60)
    try:
        for sensor in sensors:
            sensor.start()
        while True:
            stats = poll_once(sensors, gate)
            _print_poll(stats, config, once=once)
            memory.expire_tasks()
            try:
                indexed = memory.index_pending(limit=20)
            except Exception:
                logger.exception("memory indexing deferred")
                indexed = 0
            if once and indexed:
                click.echo(_line("indexed", f"{indexed} memories"))

            now = time.monotonic()
            if once or now - last_triage >= triage_interval:
                try:
                    result = triage.run_once()
                except (BudgetError, ProviderError) as exc:
                    logger.warning("triage call blocked: %s", exc)
                    if once:
                        raise click.ClickException(str(exc)) from exc
                else:
                    if once:
                        _print_triage(result)
                    elif result.ran:
                        decision = result.decision.decision if result.decision else "error"
                        click.echo(
                            f"{datetime.now():%H:%M:%S} triage events={result.events} "
                            f"decision={decision}"
                        )
                last_triage = now

            if once:
                break
            time.sleep(interval)
    except KeyboardInterrupt:
        click.echo("stopped")
    finally:
        for sensor in sensors:
            sensor.stop()
        conn.close()

    if once and stats is not None and stats.failures:
        raise click.ClickException(
            f"{stats.failures} sensor(s) failed; see {config.log_dir / 'alice.log'}"
        )


def _describe_sources(sources: tuple[Source, ...]) -> str:
    if not sources:
        return "none configured"
    parts = []
    for source in sources:
        path = source.options.get("path", "?")
        if source.type == "logfile":
            parts.append(f"logfile {source.options.get('glob', '*.log')} in {path}")
        else:
            adapter = source.options.get("adapter", "stub")
            parts.append(f"message/{adapter} {path}")
    return "; ".join(parts)


def _print_triage(result: TriageResult) -> None:
    if result.events == 0:
        click.echo(_line("triage", "no new events"))
        return
    if result.decision is None:
        click.echo(_line("triage", f"failed: {result.error or 'invalid response'}"))
        return
    decision = result.decision
    click.echo(
        _line(
            "triage",
            f"{decision.decision} p{decision.priority} events={result.events}: "
            f"{decision.reason}",
        )
    )
    if decision.suggestion_text:
        click.echo(_line("suggestion", decision.suggestion_text))


def _print_poll(stats: PollStats, config: AppSettings, *, once: bool) -> None:
    if not once:
        if stats.total or stats.failures:
            stamp = datetime.now().strftime("%H:%M:%S")
            click.echo(
                f"{stamp} observed {stats.observed}, inserted {stats.inserted}, "
                f"collapsed {stats.collapsed}, duplicates {stats.duplicates}, "
                f"above gate {stats.above_threshold}"
            )
        return

    click.echo(_line("sources", f"{stats.polls} polled, {stats.failures} failed"))
    click.echo(_line("observed", str(stats.observed)))
    click.echo(_line("inserted", str(stats.inserted)))
    click.echo(_line("collapsed", str(stats.collapsed)))
    click.echo(_line("duplicates", str(stats.duplicates)))
    click.echo(_line("redacted", str(stats.redacted)))
    click.echo(
        _line(
            "above gate",
            f"{stats.above_threshold} (min salience {config.gate.min_salience:.2f})",
        )
    )


def _line(label: str, value: str) -> str:
    return f"{label:<12}{value}"


def _scalar(conn: sqlite3.Connection, sql: str, params: tuple[object, ...]) -> int:
    row = conn.execute(sql, params).fetchone()
    return int(row[0])

