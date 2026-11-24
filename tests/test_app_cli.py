from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from alice.app.cli import main
from alice.app.settings import load_settings
from alice.core.db import connect, table_names, utc_now
from alice.models import build_router
from alice.models.base import Message
from alice.models.budget import BudgetError


def run(
    config_path: Path,
    *args: str,
    env: dict[str, str] | None = None,
    data_dir: Path | None = None,
):
    arguments = ["--config", str(config_path)]
    if data_dir is not None:
        arguments.extend(["--data-dir", str(data_dir)])
    arguments.extend(args)
    runner = CliRunner()
    return runner.invoke(
        main,
        arguments,
        env=env,
        catch_exceptions=False,
    )


def test_init_creates_database_and_seeds(config_path: Path) -> None:
    result = run(config_path, "init")

    assert result.exit_code == 0, result.output
    assert "schema      v13" in result.output
    assert (
        "migrations  applied 001_initial, 002_events_count, 003_memory, "
        "004_triage, 005_outputs, 006_consolidation, 007_hardening, 008_chat, "
        "009_suggestion_clusters, 010_actions"
        in result.output
    )

    root = config_path.parent.parent
    assert (root / "state" / "alice.db").is_file()
    assert (root / "state" / "logs" / "alice.log").is_file()
    assert (root / "state" / "raw").is_dir()
    for name in ("identity.md", "rules.md", "facts.md"):
        assert (root / "memory" / "seed" / name).is_file()


def test_init_second_run_is_a_noop_and_keeps_seed_edits(config_path: Path) -> None:
    assert run(config_path, "init").exit_code == 0

    facts = config_path.parent.parent / "memory" / "seed" / "facts.md"
    facts.write_text("# Facts\n\n- Jo works in Korean and English.\n", encoding="utf-8")

    result = run(config_path, "init")

    assert result.exit_code == 0, result.output
    assert "migrations  none pending" in result.output
    assert "Jo works in Korean and English." in facts.read_text(encoding="utf-8")


def test_status_before_init(config_path: Path) -> None:
    result = run(config_path, "status")

    assert result.exit_code == 0, result.output
    assert "not initialized (run `alice init`)" in result.output


def test_status_after_init(config_path: Path) -> None:
    assert run(config_path, "init").exit_code == 0

    result = run(config_path, "status")

    assert result.exit_code == 0, result.output
    assert "schema      v13 (up to date)" in result.output
    assert "tables      19" in result.output
    assert "events      0" in result.output
    assert "spend today $0.0000 over 0 calls" in result.output


def test_status_reports_counts_and_spend(config_path: Path) -> None:
    assert run(config_path, "init").exit_code == 0

    root = config_path.parent.parent
    conn = connect(root / "state" / "alice.db", create=False)
    try:
        conn.execute(
            "INSERT INTO events (ts, source, kind, content_hash, text_redacted) "
            "VALUES (?, 'logfile:app.log', 'log', 'h1', 'disk error')",
            (utc_now(),),
        )
        conn.execute(
            "INSERT INTO suggestions (created, priority, text) VALUES (?, 2, 'check disk')",
            (utc_now(),),
        )
        conn.execute(
            "INSERT INTO tasks (goal, created, expires) VALUES ('check disk', ?, ?)",
            (utc_now(), "2099-01-01T00:00:00Z"),
        )
        conn.execute(
            "INSERT INTO usage (ts, tier, model, input_tokens, output_tokens, cost_usd) "
            "VALUES (?, 'triage', 'fake-triage', 100, 20, 0.25)",
            (utc_now(),),
        )
        conn.commit()
        assert set(table_names(conn)) == {
            "actions",
            "ambient_alerts",
            "backup_runs",
            "budget_alerts",
            "chat_messages",
            "consolidation_runs",
            "cursors",
            "events",
            "growth_memories",
            "growth_sessions",
            "hard_memory",
            "memory_embeddings",
            "proposals",
            "suggestions",
            "summaries",
            "tasks",
            "tool_calls",
            "triage_batches",
            "usage",
        }
    finally:
        conn.close()

    result = run(config_path, "status")

    assert result.exit_code == 0, result.output
    assert "events      1" in result.output
    assert "1 total, 1 open" in result.output
    assert "1 total, 1 queued" in result.output
    assert "spend today $0.2500 over 1 calls" in result.output


def test_missing_config_exits_with_message(config_path: Path) -> None:
    result = run(config_path.parent / "missing.yaml", "status")

    assert result.exit_code == 1
    assert "Error: config file not found" in result.output


def test_bad_config_exits_with_message(alice_root: Path) -> None:
    bad = alice_root / "config" / "bad.yaml"
    bad.write_text("budget: {soft_daily_usd: 1}\n", encoding="utf-8")

    result = run(bad, "status")

    assert result.exit_code == 1
    assert "Error: missing required section: models" in result.output


def test_init_refuses_foreign_database(config_path: Path) -> None:
    root = config_path.parent.parent
    (root / "state").mkdir(parents=True)
    conn = sqlite3.connect(root / "state" / "alice.db")
    conn.execute("CREATE TABLE notes (body TEXT)")
    conn.execute("INSERT INTO notes VALUES ('keep me')")
    conn.commit()
    conn.close()

    result = run(config_path, "init")

    assert result.exit_code == 1
    assert "not created by Alice" in result.output

    conn = sqlite3.connect(root / "state" / "alice.db")
    try:
        assert conn.execute("SELECT body FROM notes").fetchone()[0] == "keep me"
    finally:
        conn.close()


def test_config_env_var(config_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["init"], env={"ALICE_CONFIG": str(config_path)})

    assert result.exit_code == 0, result.output
    assert (config_path.parent.parent / "state" / "alice.db").is_file()


def test_pause_and_resume(config_path: Path) -> None:
    assert run(config_path, "init").exit_code == 0
    state = config_path.parent.parent / "state"

    paused = run(config_path, "pause", "--reason", "testing")

    assert paused.exit_code == 0, paused.output
    assert "paused since" in paused.output
    assert (state / "paused.json").is_file()

    status = run(config_path, "status")
    assert "paused      yes since" in status.output
    assert "testing" in status.output

    resumed = run(config_path, "resume")

    assert resumed.exit_code == 0, resumed.output
    assert "resumed" in resumed.output
    assert not (state / "paused.json").exists()
    assert "paused      no" in run(config_path, "status").output


def test_resume_when_not_paused(config_path: Path) -> None:
    result = run(config_path, "resume")

    assert result.exit_code == 0, result.output
    assert "not paused" in result.output


def test_status_shows_soft_cap_reached(config_path: Path) -> None:
    assert run(config_path, "init").exit_code == 0
    conn = connect(config_path.parent.parent / "state" / "alice.db", create=False)
    try:
        conn.execute(
            "INSERT INTO usage (ts, tier, model, input_tokens, cached_tokens, "
            "output_tokens, cost_usd) VALUES (?, 'triage', 'fake-triage', 10, 0, 5, 0.06)",
            (utc_now(),),
        )
        conn.commit()
    finally:
        conn.close()

    output = run(config_path, "status").output

    assert "$0.0600 over 1 calls" in output
    assert "[soft cap reached]" in output


def test_init_creates_core_memory(config_path: Path) -> None:
    result = run(config_path, "init")

    assert result.exit_code == 0, result.output
    assert "core memory" in result.output
    core = config_path.parent.parent / "state" / "memory" / "core.json"
    assert core.is_file()
    assert '"name": "Alice"' in core.read_text(encoding="utf-8")


def test_status_shows_core_memory_and_proposals(config_path: Path) -> None:
    assert run(config_path, "init").exit_code == 0

    result = run(config_path, "status")

    assert "core memory" in result.output
    assert "(present" in result.output
    assert "proposals" in result.output
    assert "0 pending" in result.output


def test_approve_lists_and_approves_a_proposal(config_path: Path) -> None:
    assert run(config_path, "init").exit_code == 0
    conn = connect(config_path.parent.parent / "state" / "alice.db", create=False)
    try:
        conn.execute(
            "INSERT INTO proposals (key, proposed_value, reason, created, status) "
            "VALUES ('timezone', 'Asia/Seoul', 'observed', ?, 'pending')",
            (utc_now(),),
        )
        conn.commit()
    finally:
        conn.close()

    listed = run(config_path, "approve")
    assert "timezone = Asia/Seoul" in listed.output

    approved = run(config_path, "approve", "1")
    assert approved.exit_code == 0, approved.output
    assert "approved timezone v1" in approved.output

    conn = connect(config_path.parent.parent / "state" / "alice.db", create=False)
    try:
        row = conn.execute("SELECT * FROM hard_memory").fetchone()
        assert row["approved_by"] == "user"
        assert row["value"] == "Asia/Seoul"
    finally:
        conn.close()


def test_approve_unknown_proposal_fails(config_path: Path) -> None:
    assert run(config_path, "init").exit_code == 0

    result = run(config_path, "approve", "999")

    assert result.exit_code == 1
    assert "no pending proposal" in result.output


def test_paused_router_fails_loudly(config_path: Path) -> None:
    assert run(config_path, "init").exit_code == 0
    assert run(config_path, "pause").exit_code == 0

    config = load_settings(config_path)
    conn = connect(config.db_path, create=False)
    try:
        router = build_router(config.models, data_dir=config.data_dir, conn=conn)
        with pytest.raises(BudgetError, match="paused"):
            router.complete("triage", [Message("user", "hi")], 10)
    finally:
        conn.close()


def test_data_dir_option_moves_the_database(
    config_path: Path, tmp_path: Path
) -> None:
    target = tmp_path / "profile-data"

    created = run(config_path, "init", data_dir=target)

    assert created.exit_code == 0, created.output
    assert (target / "alice.db").is_file()
    assert not (config_path.parent.parent / "state").exists()

    status = run(config_path, "status", data_dir=target)
    assert str(target) in status.output
    assert "mode        source" in status.output


def test_data_dir_from_the_environment(
    config_path: Path, tmp_path: Path
) -> None:
    target = tmp_path / "env-data"

    result = run(config_path, "init", env={"ALICE_DATA_DIR": str(target)})

    assert result.exit_code == 0, result.output
    assert (target / "alice.db").is_file()


def test_legacy_state_is_migrated_for_a_frozen_build(
    config_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = config_path.parent.parent
    legacy = root / "state"
    legacy.mkdir()
    (legacy / "marker.txt").write_text("kept", encoding="utf-8")
    local = tmp_path / "Local"
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(local))

    result = run(config_path, "status")

    assert result.exit_code == 0, result.output
    target = local / "Alice"
    assert (target / "marker.txt").read_text(encoding="utf-8") == "kept"
    assert not legacy.exists()
    assert str(target) in result.output


def test_version_option() -> None:
    result = CliRunner().invoke(main, ["--version"])

    assert result.exit_code == 0
    assert "0.1.0" in result.output


def test_report_shows_usage_and_writes_a_file(config_path: Path) -> None:
    assert run(config_path, "init").exit_code == 0
    root = config_path.parent.parent
    conn = connect(root / "state" / "alice.db", create=False)
    try:
        conn.execute(
            "INSERT INTO usage (ts, tier, model, input_tokens, output_tokens, cost_usd) "
            "VALUES (?, 'triage', 'fake-triage', 100, 20, 0.25)",
            (utc_now(),),
        )
        conn.execute(
            "INSERT INTO events (ts, source, kind, content_hash, text_redacted) "
            "VALUES (?, 'logfile:app.log', 'log', 'h1', 'disk error')",
            (utc_now(),),
        )
        conn.commit()
    finally:
        conn.close()

    result = run(config_path, "report", "--days", "7")
    assert result.exit_code == 0, result.output
    assert "Alice report" in result.output
    assert "fake-triage" in result.output
    assert "- Model calls: 1" in result.output

    written = run(config_path, "report", "--write")
    assert written.exit_code == 0, written.output
    assert "reports" in written.output
    assert list((root / "state" / "reports").glob("report-*.md"))


def test_discover_previews_matching_logs(tmp_path: Path) -> None:
    (tmp_path / "app.log").write_text("error happened\n", encoding="utf-8")
    (tmp_path / "notes.md").write_text("not a log\n", encoding="utf-8")

    result = CliRunner().invoke(
        main,
        ["discover", "--root", str(tmp_path), "--recent-days", "0", "--limit", "5"],
    )

    assert result.exit_code == 0, result.output
    assert "app.log" in result.output
    assert "notes.md" not in result.output
    assert "1 matched" in result.output


@pytest.mark.skipif(sys.platform != "win32", reason="DPAPI is Windows-only")
def test_backup_list_and_restore(config_path: Path, tmp_path: Path) -> None:
    assert run(config_path, "init").exit_code == 0
    root = config_path.parent.parent

    result = run(config_path, "backup")
    assert result.exit_code == 0, result.output
    assert "backup" in result.output
    files = list((root / "state" / "backups").glob("*.alicebak"))
    assert len(files) == 1

    listing = run(config_path, "backup", "--list")
    assert listing.exit_code == 0, listing.output
    assert files[0].name in listing.output

    target = tmp_path / "restored"
    restored = run(
        config_path, "backup", "--restore", str(files[0]), "--to", str(target)
    )
    assert restored.exit_code == 0, restored.output
    assert "restore" in restored.output
    assert (target / "alice.db").is_file()

