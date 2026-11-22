from __future__ import annotations

from datetime import datetime
from pathlib import Path

from click.testing import CliRunner

from alice.app.cli import main
from alice.inputs.config import GateConfig, RedactionConfig, Source
from alice.core.db import connect, init_db
from alice.core.events import CursorStore, RawStore, WriteKind
from alice.inputs.gate import Gate
from alice.inputs.sensors import poll_once
from alice.inputs.sensors.logfile import LogfileSensor

FIXTURES = Path(__file__).parent / "fixtures"

PII = (
    "900101-1234567",
    "010-9876-5432",
    "010-2345-6789",
    "jane.doe@example.com",
    "john.smith@example.org",
    "4111-1111-1111-1111",
    "110-123-456789",
    "02-555-1234",
)


def write_config(root: Path, *, log_dir: Path, messages: Path) -> Path:
    config_dir = root / "config"
    config_dir.mkdir(parents=True)
    config_path = config_dir / "alice.yaml"
    config_path.write_text(
        f"""\
models:
  triage:      {{provider: fake, model: fake-triage}}
  reason:      {{provider: fake, model: fake-reason}}
  consolidate: {{provider: fake, model: fake-consolidate, batch: true}}

sources:
  - {{type: logfile, path: '{log_dir.as_posix()}', glob: '*.log', from_start: true}}
  - {{type: message, adapter: stub, path: '{messages.as_posix()}'}}

gate:
  min_salience: 0.5
""",
        encoding="utf-8",
    )
    return config_path


def setup_fixtures(root: Path) -> tuple[Path, Path]:
    logs = root / "logs"
    logs.mkdir()
    (logs / "app_en.log").write_bytes((FIXTURES / "logs" / "app_en.log").read_bytes())
    (logs / "app_ko.log").write_bytes((FIXTURES / "logs" / "app_ko.log").read_bytes())
    messages = root / "messages.txt"
    messages.write_bytes((FIXTURES / "messages_stub.txt").read_bytes())
    return logs, messages


def invoke(config_path: Path, *args: str):
    runner = CliRunner()
    return runner.invoke(
        main, ["--config", str(config_path), *args], catch_exceptions=False
    )


def test_run_once_initializes_the_ledger(tmp_path: Path) -> None:
    root = tmp_path / "alice"
    root.mkdir()
    logs, messages = setup_fixtures(root)
    config_path = write_config(root, log_dir=logs, messages=messages)

    assert invoke(config_path, "init").exit_code == 0
    result = invoke(config_path, "run", "--once", "--dry-run")

    assert result.exit_code == 0, result.output
    assert "inserted" in result.output
    assert "above gate" in result.output

    db_path = root / "state" / "alice.db"
    conn = connect(db_path, create=False)
    try:
        rows = conn.execute("SELECT * FROM events ORDER BY id").fetchall()
        assert rows
        stored = "\n".join(row["text_redacted"] for row in rows)
        for needle in PII:
            assert needle not in stored
        assert "[RRN]" in stored
        assert "[EMAIL]" in stored
        assert "[CARD]" in stored
        assert any(row["raw_ref"] for row in rows)
        assert max(row["salience"] for row in rows) >= 0.5
        cursors = conn.execute("SELECT COUNT(*) FROM cursors").fetchone()[0]
        assert cursors == 3
    finally:
        conn.close()


def test_second_run_and_restart_produce_no_duplicates(tmp_path: Path) -> None:
    root = tmp_path / "alice"
    root.mkdir()
    logs, messages = setup_fixtures(root)
    config_path = write_config(root, log_dir=logs, messages=messages)
    assert invoke(config_path, "init").exit_code == 0
    assert invoke(config_path, "run", "--once", "--dry-run").exit_code == 0

    db_path = root / "state" / "alice.db"
    conn = connect(db_path, create=False)
    try:
        before = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        count_before = conn.execute("SELECT SUM(count) FROM events").fetchone()[0]
    finally:
        conn.close()

    for _ in range(2):
        result = invoke(config_path, "run", "--once", "--dry-run")
        assert result.exit_code == 0, result.output
        assert "inserted    0" in result.output

    conn = connect(db_path, create=False)
    try:
        assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == before
        assert conn.execute("SELECT SUM(count) FROM events").fetchone()[0] == count_before
    finally:
        conn.close()


def test_new_log_lines_are_picked_up(tmp_path: Path) -> None:
    root = tmp_path / "alice"
    root.mkdir()
    logs, messages = setup_fixtures(root)
    config_path = write_config(root, log_dir=logs, messages=messages)
    assert invoke(config_path, "init").exit_code == 0
    assert invoke(config_path, "run", "--once", "--dry-run").exit_code == 0

    with (logs / "app_en.log").open("a", encoding="utf-8") as handle:
        handle.write("2026-11-20T09:00:00Z ERROR zebra widget exploded\n")

    result = invoke(config_path, "run", "--once", "--dry-run")

    assert result.exit_code == 0, result.output
    assert "inserted    1" in result.output


def test_dry_run_triages_and_updates_the_ledger(tmp_path: Path) -> None:
    root = tmp_path / "alice"
    root.mkdir()
    logs, messages = setup_fixtures(root)
    config_path = write_config(root, log_dir=logs, messages=messages)
    assert invoke(config_path, "init").exit_code == 0

    result = invoke(config_path, "run", "--once", "--dry-run")

    assert result.exit_code == 0, result.output
    assert "triage" in result.output
    conn = connect(root / "state" / "alice.db", create=False)
    try:
        assert conn.execute("SELECT COUNT(*) FROM triage_batches").fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM events WHERE status = 'done'"
        ).fetchone()[0] > 0
        assert conn.execute(
            "SELECT COUNT(*) FROM suggestions WHERE status = 'queued'"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM events WHERE status = 'new' AND salience >= 0.5"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM events WHERE status = 'new'"
        ).fetchone()[0] > 0
    finally:
        conn.close()

    digest = root / "state" / "digests" / f"{datetime.now().astimezone().date().isoformat()}.md"
    assert digest.is_file()
    assert "Dry run suggestion" in digest.read_text(encoding="utf-8")


def test_run_requires_an_initialized_database(tmp_path: Path) -> None:
    root = tmp_path / "alice"
    root.mkdir()
    logs, messages = setup_fixtures(root)
    config_path = write_config(root, log_dir=logs, messages=messages)

    result = invoke(config_path, "run", "--once", "--dry-run")

    assert result.exit_code == 1
    assert "run `alice init` first" in result.output


def test_uncommitted_cursor_reingest_is_deduped(tmp_path: Path) -> None:
    """A crash after the ledger write but before cursor commit must not duplicate."""
    root = tmp_path / "alice"
    logs = root / "logs"
    logs.mkdir(parents=True)
    (logs / "app.log").write_text("alpha\nbeta\n", encoding="utf-8")

    db_path = root / "state" / "alice.db"
    init_db(db_path)
    conn = connect(db_path, create=False)
    try:
        cursors = CursorStore(conn)
        gate = Gate(
            conn,
            gate=GateConfig(),
            redaction=RedactionConfig(),
            raw_store=RawStore(root / "state" / "raw"),
        )
        source = Source(
            type="logfile",
            options={"path": str(logs), "from_start": True},
        )

        first = LogfileSensor(source, root=root, cursors=cursors)
        gate.ingest_many(first.poll())  # no first.commit(): simulate a crash

        restarted = LogfileSensor(source, root=root, cursors=cursors)
        results = gate.ingest_many(restarted.poll())

        assert [result.write.kind for result in results] == [
            WriteKind.DUPLICATE,
            WriteKind.DUPLICATE,
        ]
        assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 2
    finally:
        conn.close()


def test_pii_scan_of_every_stored_event(tmp_path: Path) -> None:
    root = tmp_path / "alice"
    root.mkdir()
    logs, messages = setup_fixtures(root)
    config_path = write_config(root, log_dir=logs, messages=messages)
    assert invoke(config_path, "init").exit_code == 0
    assert invoke(config_path, "run", "--once", "--dry-run").exit_code == 0

    conn = connect(root / "state" / "alice.db", create=False)
    try:
        rows = conn.execute("SELECT text_redacted FROM events").fetchall()
        assert rows
        for row in rows:
            text = row["text_redacted"]
            for needle in PII:
                assert needle not in text
    finally:
        conn.close()

    log_text = (root / "state" / "logs" / "alice.log").read_text(encoding="utf-8")
    for needle in PII:
        assert needle not in log_text


def test_consolidate_dry_run_writes_summaries(tmp_path: Path) -> None:
    root = tmp_path / "alice"
    root.mkdir()
    logs, messages = setup_fixtures(root)
    config_path = write_config(root, log_dir=logs, messages=messages)
    assert invoke(config_path, "init").exit_code == 0
    assert invoke(config_path, "run", "--once", "--dry-run").exit_code == 0

    today = datetime.now().astimezone().date().isoformat()
    result = invoke(
        config_path, "consolidate", "--day", today, "--dry-run"
    )

    assert result.exit_code == 0, result.output
    assert "consolidate" in result.output
    conn = connect(root / "state" / "alice.db", create=False)
    try:
        assert conn.execute("SELECT COUNT(*) FROM summaries").fetchone()[0] > 0
        run = conn.execute(
            "SELECT * FROM consolidation_runs WHERE day = ?", (today,)
        ).fetchone()
        assert run is not None
        assert run["summary_count"] > 0
        assert conn.execute(
            "SELECT COUNT(*) FROM proposals WHERE status = 'pending'"
        ).fetchone()[0] > 0
    finally:
        conn.close()


def test_logscan_once_reads_existing_and_new_lines(tmp_path: Path) -> None:
    root = tmp_path / "alice"
    root.mkdir()
    logs = tmp_path / "outside" / "logs"
    logs.mkdir(parents=True)
    log = logs / "app.log"
    log.write_text("first failure\n", encoding="utf-8")
    config_dir = root / "config"
    config_dir.mkdir()
    config_path = config_dir / "alice.yaml"
    config_path.write_text(
        f"""\
models:
  triage:      {{provider: fake, model: fake-triage}}
  reason:      {{provider: fake, model: fake-reason}}
  consolidate: {{provider: fake, model: fake-consolidate}}
sources:
  - type: logscan
    path: '{logs.as_posix()}'
    globs: ['*.log']
    from_start: true
    recent_days: 0
gate:
  min_salience: 0.5
""",
        encoding="utf-8",
    )

    assert invoke(config_path, "init").exit_code == 0
    first = invoke(config_path, "run", "--once", "--dry-run")
    assert first.exit_code == 0, first.output
    assert "inserted    1" in first.output

    with log.open("a", encoding="utf-8") as handle:
        handle.write("second failure\n")
    second = invoke(config_path, "run", "--once", "--dry-run")
    assert second.exit_code == 0, second.output
    assert "inserted    1" in second.output

    conn = connect(root / "state" / "alice.db", create=False)
    try:
        texts = [
            row["text_redacted"]
            for row in conn.execute(
                "SELECT text_redacted FROM events "
                "WHERE source LIKE 'logfile:%' ORDER BY id"
            )
        ]
    finally:
        conn.close()
    assert texts == ["first failure", "second failure"]


def test_poll_once_counts_duplicates(tmp_path: Path) -> None:
    """The Korean fixture repeats one line; the ledger must drop it."""
    root = tmp_path / "alice"
    root.mkdir()
    logs, messages = setup_fixtures(root)
    config_path = write_config(root, log_dir=logs, messages=messages)
    assert invoke(config_path, "init").exit_code == 0

    result = invoke(config_path, "run", "--once", "--dry-run")

    assert result.exit_code == 0, result.output
    assert "duplicates  1" in result.output
    assert "collapsed   2" in result.output


class _FailingGate:
    def ingest_many(self, events):
        raise RuntimeError("simulated failure")


def test_failed_ingest_does_not_advance_the_cursor(tmp_path: Path) -> None:
    root = tmp_path / "alice"
    logs = root / "logs"
    logs.mkdir(parents=True)
    (logs / "app.log").write_text("alpha\nbeta\n", encoding="utf-8")

    db_path = root / "state" / "alice.db"
    init_db(db_path)
    conn = connect(db_path, create=False)
    try:
        cursors = CursorStore(conn)
        source = Source(type="logfile", options={"path": str(logs), "from_start": True})
        sensor = LogfileSensor(source, root=root, cursors=cursors)

        failed = poll_once([sensor], _FailingGate())  # type: ignore[arg-type]
        assert failed.failures == 1
        assert cursors.count() == 0

        gate = Gate(
            conn,
            gate=GateConfig(),
            redaction=RedactionConfig(),
            raw_store=RawStore(root / "state" / "raw"),
        )
        retried = poll_once([sensor], gate)

        assert retried.inserted == 2
        assert cursors.count() == 1
    finally:
        conn.close()

