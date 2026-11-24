from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from alice.app.dashboard import load_dashboard
from alice.app.settings import load_settings
from alice.contracts.outputs import Suggestion
from alice.core.db import connect, init_db, utc_now
from alice.memory.core_file import CoreMemory
from alice.outputs.queue import SuggestionQueue


@pytest.fixture
def conn(config_path: Path):
    settings = load_settings(config_path)
    init_db(settings.db_path)
    CoreMemory(
        settings.memory.core_memory_file, settings.memory.hard_max_tokens
    ).ensure()
    connection = connect(settings.db_path, create=False)
    yield settings, connection
    connection.close()


def test_dashboard_snapshot(conn) -> None:
    settings, connection = conn
    connection.execute(
        "INSERT INTO events (ts, source, kind, content_hash, text_redacted, salience, status) "
        "VALUES (?, 'logfile:app.log', 'log', 'h1', 'disk failure', 0.9, 'escalated')",
        (utc_now(),),
    )
    SuggestionQueue(connection).submit(
        Suggestion("check the disk", 1, (1,), "disk warning")
    )
    connection.execute(
        "INSERT INTO proposals (key, proposed_value, reason, created, status) "
        "VALUES ('timezone', 'Asia/Seoul', 'observed', ?, 'pending')",
        (utc_now(),),
    )
    connection.execute(
        "INSERT INTO triage_batches (created, event_ids, decision, priority, reason, attempts) "
        "VALUES (?, '[1]', 'escalate', 1, 'ambiguous', 1)",
        (utc_now(),),
    )
    connection.execute(
        "INSERT INTO consolidation_runs (day, created, summary_count, proposal_count) "
        "VALUES ('2026-11-21', ?, 3, 1)",
        (utc_now(),),
    )
    connection.execute(
        "INSERT INTO usage (ts, tier, model, input_tokens, cached_tokens, "
        "output_tokens, cost_usd, purpose) "
        "VALUES (?, 'reason', 'fake', 100, 0, 20, 0.0, 'test')",
        (utc_now(),),
    )
    connection.execute(
        "INSERT INTO tool_calls (created, tool, args, ok, summary, duration_ms) "
        "VALUES (?, 'time.now', '{}', 1, 'Local: 12:00', 5)",
        (utc_now(),),
    )
    connection.execute(
        "INSERT INTO ambient_alerts (created, kind, alert_key, title, body) "
        "VALUES (?, 'calendar', 'review', 'In 10 minutes: review', 'In 10 minutes: review')",
        (utc_now(),),
    )
    connection.commit()
    today = datetime.now().astimezone().date().isoformat()
    digest = settings.data_dir / "digests" / f"{today}.md"
    digest.parent.mkdir(parents=True, exist_ok=True)
    digest.write_text("# today\n", encoding="utf-8")

    dashboard = load_dashboard(settings, connection)

    assert dashboard.schema_version == 14
    assert dashboard.status_counts["escalated"] == 1
    assert dashboard.new_above_gate == 0
    assert dashboard.queued_suggestions == 1
    assert dashboard.pending_proposals == 1
    assert dashboard.pending_escalations == 1
    assert dashboard.paused is None
    assert dashboard.events[0].text == "disk failure"
    assert dashboard.suggestions[0].text == "check the disk"
    assert dashboard.proposals[0].key == "timezone"
    assert dashboard.core_tokens > 0
    assert dashboard.digest_text == "# today\n"
    assert dashboard.last_consolidation == "2026-11-21"
    assert dashboard.usage_total.calls == 1
    assert dashboard.usage_total.total_tokens == 120
    assert dashboard.tool_calls_total == 1
    assert dashboard.recent_tool_calls[0].tool == "time.now"
    assert dashboard.tools
    assert dashboard.hardware["cpu_count"] >= 1
    assert dashboard.core_max_tokens > 0
    assert dashboard.plugins == ()
    assert dashboard.growth_memories == ()
    assert dashboard.growth_stats["sessions"] == 0
    assert dashboard.ambient_alerts[0].kind == "calendar"
    assert dashboard.activity is None or "category" in dashboard.activity
    assert dashboard.monitor_state is None


def test_gui_module_imports() -> None:
    from alice.app import gui

    assert callable(gui.run_gui)
    assert callable(gui.main)
