"""Alice command line: init, run, status, approve, report.

M2 ships `init`, `status`, sensor-only `run`, and `pause`/`resume` for the
model budget kill switch. Triage, approval, and the cost report arrive with
their milestones.
"""

from __future__ import annotations

import logging
import sqlite3
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

import click

from .. import __version__
from ..brain.consolidate import ConsolidationResult
from ..brain.reason import ReasoningResult
from ..brain.triage import TriageResult
from ..core.backup import (
    BackupError,
    BackupResult,
    RestoreResult,
    format_size,
    restore_backup,
)
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
from ..core.startup import (
    StartupError,
    install_startup,
    remove_startup,
    startup_status,
)
from ..inputs import PollStats, poll_once
from ..inputs.config import Source
from ..inputs.sensors.logscan import coerce_globs, discover_log_files
from ..memory.core_file import CoreMemory
from ..memory.errors import CoreMemoryError, MemoryCapError, ProposalError
from ..memory.seed import SEED_TEMPLATES, ensure_seed_files
from ..core.tokens import estimate_tokens
from ..models.base import ProviderError, ProviderNotConfigured
from ..models import RouterError
from ..models.budget import BudgetError, BudgetGuard, clear_pause, pause_file, set_pause
from ..outputs.report import render_report, write_report
from .bootstrap import (
    build_backup,
    build_consolidation,
    build_digest,
    build_ingestion,
    build_memory,
    build_models,
    build_notifications,
    build_queue,
    build_reasoning,
    build_triage,
)
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
    # A model may return characters the Windows console code page cannot encode.
    # Replace them instead of crashing the whole command.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass
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
        if state.version is not None and state.version >= 5:
            pending_reason = _scalar(
                conn,
                "SELECT COUNT(*) FROM triage_batches WHERE decision = 'escalate' "
                "AND reasoned_at IS NULL",
                (),
            )
            click.echo(_line("reasoning", f"{pending_reason} escalations pending"))
            digest_path = (
                config.data_dir
                / "digests"
                / f"{datetime.now().astimezone().date().isoformat()}.md"
            )
            click.echo(
                _line(
                    "digest",
                    f"{digest_path} "
                    f"({'present' if digest_path.is_file() else 'not written yet'})",
                )
            )
        if state.version is not None and state.version >= 6:
            row = conn.execute(
                "SELECT day, summary_count, proposal_count FROM consolidation_runs "
                "ORDER BY day DESC LIMIT 1"
            ).fetchone()
            if row is None:
                click.echo(_line("consolidate", "none yet"))
            else:
                click.echo(
                    _line(
                        "consolidate",
                        f"last {row['day']}: {row['summary_count']} summaries, "
                        f"{row['proposal_count']} proposals",
                    )
                )
        if state.version is not None and state.version >= 7:
            row = conn.execute(
                "SELECT day, path, size_bytes FROM backup_runs "
                "ORDER BY day DESC LIMIT 1"
            ).fetchone()
            if row is None:
                click.echo(_line("backup", "none yet"))
            else:
                click.echo(
                    _line(
                        "backup",
                        f"last {row['day']}: {row['path']} "
                        f"({format_size(int(row['size_bytes']))})",
                    )
                )
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
@click.option(
    "--root",
    type=click.Path(path_type=Path, exists=True),
    default=Path("C:/"),
    show_default=True,
    help="Directory to scan.",
)
@click.option(
    "--globs",
    default="*.log,*.txt,*.jsonl",
    show_default=True,
    help="Comma-separated filename patterns.",
)
@click.option("--max-files", type=click.IntRange(min=1), default=5000, show_default=True)
@click.option("--recent-days", type=click.IntRange(min=0), default=14, show_default=True)
@click.option("--max-depth", type=click.IntRange(min=1), default=20, show_default=True)
@click.option("--limit", type=click.IntRange(min=0, max=200), default=20, show_default=True)
def discover(
    root: Path,
    globs: str,
    max_files: int,
    recent_days: int,
    max_depth: int,
    limit: int,
) -> None:
    """Preview the files a log scan would track. Read-only."""
    click.echo(_line("scan", f"{root} (last {recent_days} days, max {max_files} files)"))
    result = discover_log_files(
        root,
        globs=coerce_globs(globs),
        max_files=max_files,
        recent_days=recent_days,
        max_depth=max_depth,
        text_only=True,
        progress=lambda dirs, files, errors: click.echo(
            f"  scanned {dirs} dirs, {files} files, {errors} unreadable",
            err=True,
        ),
    )
    counts: dict[str, int] = {}
    for path in result.files:
        suffix = path.suffix.lower() or "(none)"
        counts[suffix] = counts.get(suffix, 0) + 1
    click.echo(_line("files", f"{len(result.files)} matched"))
    click.echo(_line("dirs", f"{result.dirs_seen} scanned, {result.errors} unreadable"))
    if result.truncated:
        click.echo(_line("cap", f"stopped at {max_files}; raise max_files or narrow the root"))
    if counts:
        by_type = ", ".join(f"{suffix} {count}" for suffix, count in sorted(counts.items()))
        click.echo(_line("by type", by_type))
    if limit and result.files:
        click.echo(_line("sample", ""))
        for path in result.files[:limit]:
            click.echo(f"  {path}")
    click.echo(
        _line(
            "note",
            "from_start defaults to false, so existing lines are not replayed",
        )
    )


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
@click.pass_context
def gui(ctx: click.Context) -> None:
    """Open the read-only desktop dashboard."""
    config = _load_config(ctx)
    _prepare_runtime(config)
    setup_logging(config.log_dir, verbose=ctx.obj["verbose"])
    from .gui import run_gui

    try:
        run_gui(config.path, config.data_dir)
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc


@main.command()
@click.option("--day", "day_text", default=None, help="Local day YYYY-MM-DD (default: yesterday).")
@click.option("--dry-run", is_flag=True, help="Use the fake provider.")
@click.pass_context
def consolidate(ctx: click.Context, day_text: str | None, dry_run: bool) -> None:
    """Summarize one day, propose memories, and prune old raw payloads."""
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
    target = None
    if day_text:
        try:
            target = date.fromisoformat(day_text)
        except ValueError as exc:
            raise click.ClickException("--day must be YYYY-MM-DD") from exc

    conn = connect(config.db_path, create=False)
    try:
        memory = build_memory(config, conn)
        try:
            router = build_models(config, conn, dry_run=dry_run)
        except (ProviderNotConfigured, RouterError) as exc:
            raise click.ClickException(str(exc)) from exc
        service = build_consolidation(config, conn, router, memory)
        try:
            result = service.run(target, force=True)
        except (BudgetError, ProviderError) as exc:
            raise click.ClickException(str(exc)) from exc
        _print_consolidation(result)
    finally:
        conn.close()


@main.command("backup")
@click.option(
    "--restore",
    "restore_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Restore this .alicebak file instead of creating a backup.",
)
@click.option(
    "--to",
    "target",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Restore into this data directory (default: the configured one).",
)
@click.option("--force", is_flag=True, help="Replace an existing database on restore.")
@click.option("--list", "list_only", is_flag=True, help="List local backup files.")
@click.pass_context
def backup_cmd(
    ctx: click.Context,
    restore_path: Path | None,
    target: Path | None,
    force: bool,
    list_only: bool,
) -> None:
    """Create an encrypted backup, list backups, or restore one."""
    config = _load_config(ctx)
    _prepare_runtime(config)
    setup_logging(config.log_dir, verbose=ctx.obj["verbose"])

    if restore_path is not None:
        destination = target or config.data_dir
        try:
            restored = restore_backup(restore_path, destination, force=force)
        except BackupError as exc:
            raise click.ClickException(str(exc)) from exc
        _print_restore(restored)
        return

    if list_only:
        files = (
            sorted(
                config.core.backup.dir.glob("alice-*.alicebak"),
                key=lambda item: item.name,
                reverse=True,
            )
            if config.core.backup.dir.is_dir()
            else []
        )
        if not files:
            click.echo(_line("backups", f"none in {config.core.backup.dir}"))
            return
        click.echo(_line("backups", f"{len(files)} in {config.core.backup.dir}"))
        for path in files:
            stamp = datetime.fromtimestamp(path.stat().st_mtime).strftime(
                "%Y-%m-%d %H:%M"
            )
            click.echo(
                _line("  file", f"{path.name}  {format_size(path.stat().st_size)}  {stamp}")
            )
        return

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
        service = build_backup(config, conn)
        try:
            result = service.run(force=True)
        except BackupError as exc:
            raise click.ClickException(str(exc)) from exc
    finally:
        conn.close()
    _print_backup(result)


@main.command()
@click.option(
    "--days",
    type=click.IntRange(1, 365),
    default=7,
    show_default=True,
    help="How many days to include.",
)
@click.option("--write", is_flag=True, help="Write the report under reports/.")
@click.pass_context
def report(ctx: click.Context, days: int, write: bool) -> None:
    """Show weekly cost and activity trends without calling a model."""
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
        if write:
            result = write_report(conn, config.data_dir, days=days)
            click.echo(
                _line("report", f"{result.path} ({'created' if result.created else 'updated'})")
            )
        else:
            click.echo(render_report(conn, days=days))
    finally:
        conn.close()


@main.command()
@click.option("--install", "action", flag_value="install", help="Register the logon task.")
@click.option("--remove", "action", flag_value="remove", help="Remove the logon task.")
@click.option(
    "--status",
    "action",
    flag_value="status",
    default="status",
    help="Show whether the logon task exists (default).",
)
@click.option("--user", default=None, help="Windows user for the logon trigger.")
@click.pass_context
def startup(ctx: click.Context, action: str, user: str | None) -> None:
    """Manage the hidden logon Scheduled Task."""
    config = _load_config(ctx)
    _prepare_runtime(config)
    try:
        if action == "install":
            if not getattr(sys, "frozen", False):
                raise click.ClickException(
                    "install the packaged build, then run `alice startup --install` "
                    "from that folder"
                )
            exe = Path(sys.executable)
            configured = config.path
            default = default_config_path().expanduser().resolve()
            config_arg = None if configured == default else configured
            status = install_startup(
                exe, config_path=config_arg, user=user
            )
        elif action == "remove":
            status = remove_startup()
        else:
            status = startup_status()
    except StartupError as exc:
        raise click.ClickException(str(exc)) from exc

    if action == "install":
        click.echo(_line("startup", f"installed at logon ({status.task})"))
    elif action == "remove":
        click.echo(_line("startup", f"removed ({status.task})"))
    else:
        state = "installed" if status.installed else "not installed"
        click.echo(_line("startup", f"{state} ({status.task}: {status.state})"))


@main.command()
@click.option(
    "--lines",
    type=click.IntRange(min=1),
    default=2000,
    show_default=True,
    help="Maximum lines read per source per poll.",
)
@click.option(
    "--max-lines",
    type=click.IntRange(min=0),
    default=50000,
    show_default=True,
    help="Stop after this many observed lines; 0 means unlimited.",
)
@click.option(
    "--max-polls",
    type=click.IntRange(min=1),
    default=500,
    show_default=True,
    help="Safety limit on the number of poll cycles.",
)
@click.option(
    "--sleep",
    type=click.FloatRange(min=0),
    default=0.5,
    show_default=True,
    help="Seconds between polling cycles.",
)
@click.pass_context
def backfill(
    ctx: click.Context,
    lines: int,
    max_lines: int,
    max_polls: int,
    sleep: float,
) -> None:
    """Read existing logs into the ledger without calling a model."""
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
    try:
        ingestion = build_ingestion(
            config, conn, from_start=True, max_lines=lines
        )
        sensors, gate = ingestion.sensors, ingestion.gate
        if not sensors:
            raise click.ClickException(
                "no sources configured; add one under `sources:` in config/alice.yaml"
            )

        click.echo(
            "backfill mode: no model calls; normal `alice run` will triage the events"
        )
        if any(source.type == "logscan" for source in config.sources):
            click.echo("scanning configured log roots; this can take 30-80 seconds...")

        total = PollStats()
        polls = 0
        idle = 0
        reason = "poll limit reached"
        try:
            for sensor in sensors:
                sensor.start()
            while polls < max_polls:
                stats = poll_once(sensors, gate)
                polls += 1
                _add_stats(total, stats)
                click.echo(
                    f"{datetime.now():%H:%M:%S} backfill poll {polls}: "
                    f"observed {total.observed}, inserted {total.inserted}, "
                    f"collapsed {total.collapsed}, duplicates {total.duplicates}, "
                    f"above gate {total.above_threshold}"
                )
                if stats.observed == 0:
                    idle += 1
                    if idle >= 2:
                        reason = "caught up"
                        break
                else:
                    idle = 0
                if max_lines and total.observed >= max_lines:
                    reason = "line budget reached"
                    break
                if sleep:
                    time.sleep(sleep)
        except KeyboardInterrupt:
            reason = "interrupted"
            click.echo("stopped")
        finally:
            for sensor in sensors:
                sensor.stop()
    finally:
        conn.close()

    click.echo(
        _line(
            "backfill",
            f"{polls} polls, {total.observed} observed, {total.inserted} inserted, "
            f"{total.collapsed} collapsed, {total.duplicates} duplicates, "
            f"{total.above_threshold} above gate ({reason})",
        )
    )
    if total.failures:
        click.echo(_line("failures", f"{total.failures} source(s) failed; see the log"))


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
    queue = build_queue(conn)
    try:
        router = build_models(config, conn, dry_run=dry_run)
        triage = build_triage(config, conn, router, memory, queue)
    except (ProviderNotConfigured, RouterError) as exc:
        conn.close()
        raise click.ClickException(str(exc)) from exc
    reasoning = build_reasoning(config, conn, router, memory, queue)
    consolidation = build_consolidation(config, conn, router, memory)
    notifier = build_notifications(config, dry_run=dry_run)
    guard = BudgetGuard(conn, config.models.budget, config.data_dir)
    digest = build_digest(config, conn, guard)
    backup = build_backup(config, conn)
    if any(source.type == "logscan" for source in config.sources):
        click.echo(
            "scanning configured log roots; the first pass can take 30-80 seconds..."
        )
    if not sensors:
        conn.close()
        raise click.ClickException(
            "no sources configured; add one under `sources:` in config/alice.yaml"
        )

    stats: PollStats | None = None
    last_triage = 0.0
    last_consolidation = 0.0
    last_backup = 0.0
    triage_interval = max(1.0, config.brain.triage.interval_minutes * 60)
    heartbeat = config.data_dir / "run.heartbeat"
    _write_heartbeat(heartbeat)
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

            for _ in range(min(6, max(1, config.models.budget.max_escalations_per_hour))):
                try:
                    reasoning_result = reasoning.run_once()
                except (BudgetError, ProviderError) as exc:
                    logger.warning("reasoning call blocked: %s", exc)
                    if once:
                        raise click.ClickException(str(exc)) from exc
                    break
                if not reasoning_result.ran:
                    break
                if once:
                    _print_reasoning(reasoning_result)
                else:
                    click.echo(
                        f"{datetime.now():%H:%M:%S} reasoning batch="
                        f"{reasoning_result.batch_id} suggestions="
                        f"{reasoning_result.suggestions}"
                    )

            local_now = datetime.now().astimezone()
            if (
                local_now.hour >= config.brain.consolidation.hour
                and time.monotonic() - last_consolidation >= 3600
            ):
                try:
                    consolidation_result = consolidation.run(now=local_now)
                except (BudgetError, ProviderError) as exc:
                    logger.warning("consolidation call blocked: %s", exc)
                    if once:
                        raise click.ClickException(str(exc)) from exc
                else:
                    if once and consolidation_result.ran:
                        _print_consolidation(consolidation_result)
                last_consolidation = time.monotonic()

            if (
                config.core.backup.enabled
                and local_now.hour >= config.core.backup.hour
                and time.monotonic() - last_backup >= 3600
            ):
                try:
                    backup_result = backup.run(now=local_now)
                except BackupError as exc:
                    logger.warning("backup skipped: %s", exc)
                else:
                    if once and backup_result.ran:
                        _print_backup(backup_result)
                    elif once:
                        click.echo(_line("backup", backup_result.reason or "skipped"))
                last_backup = time.monotonic()

            delivered = queue.deliver(notifier, config.outputs.notify)
            if once and delivered:
                click.echo(_line("toasts", str(delivered)))
            digest_result = digest.write(notifier=notifier)
            if once:
                _print_digest(digest_result)

            _write_heartbeat(heartbeat)
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
        elif source.type == "logscan":
            globs = source.options.get("globs", source.options.get("glob", "*.log"))
            if isinstance(globs, (list, tuple)):
                globs = ",".join(str(item) for item in globs)
            cap = source.options.get("max_files", 5000)
            parts.append(f"logscan {globs} under {path} (max {cap})")
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


def _print_reasoning(result: ReasoningResult) -> None:
    if not result.ran:
        return
    if result.error:
        click.echo(_line("reasoning", f"failed: {result.error}"))
        return
    heavy = " heavy" if result.used_heavy else ""
    click.echo(
        _line(
            "reasoning",
            f"batch {result.batch_id} -> {result.suggestions} suggestions{heavy}",
        )
    )


def _print_digest(result) -> None:
    state = "created" if result.created else "updated"
    click.echo(_line("digest", f"{result.path} ({state})"))
    if result.notified:
        click.echo(_line("toast", result.summary))


def _print_consolidation(result: ConsolidationResult) -> None:
    if not result.ran:
        return
    detail = (
        f"{result.day}: {result.hours} hourly, {result.proposals} proposals, "
        f"{result.raw_deleted} raw pruned"
    )
    if result.error:
        detail += f" (error: {result.error})"
    click.echo(_line("consolidate", detail))


def _print_backup(result: BackupResult) -> None:
    if not result.ran or result.path is None:
        click.echo(_line("backup", result.reason or "skipped"))
        return
    click.echo(
        _line(
            "backup",
            f"{result.path} ({format_size(result.size_bytes)}); "
            f"{result.kept} kept",
        )
    )


def _print_restore(result: RestoreResult) -> None:
    detail = (
        f"{result.target} (schema v{result.schema}, "
        f"{', '.join(result.files)})"
    )
    if result.day:
        detail += f" from {result.day}"
    click.echo(_line("restore", detail))


def _write_heartbeat(path: Path) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            encoding="utf-8",
        )
    except OSError:
        logger.warning("could not write heartbeat: %s", path)


_STAT_FIELDS = (
    "polls",
    "observed",
    "inserted",
    "collapsed",
    "duplicates",
    "above_threshold",
    "redacted",
    "failures",
)


def _add_stats(total: PollStats, stats: PollStats) -> None:
    for name in _STAT_FIELDS:
        setattr(total, name, getattr(total, name) + getattr(stats, name))


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

