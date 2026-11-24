from __future__ import annotations

from pathlib import Path

import pytest

from alice.app.settings import load_settings
from alice.app.system_info import snapshot
from alice.app.tools import build_tools, ensure_calendar_file
from alice.core.db import connect, init_db


@pytest.fixture
def tools(config_path: Path):
    settings = load_settings(config_path)
    init_db(settings.db_path)
    ensure_calendar_file(settings.tools.calendar_file)
    connection = connect(settings.db_path, create=False)
    yield settings, build_tools(settings, connection)
    connection.close()


def test_tool_catalog_is_read_only(tools) -> None:
    _settings, registry = tools
    specs = registry.specs()
    names = [spec.name for spec in specs]
    assert "time.now" in names
    assert "system.snapshot" in names
    assert "activity.snapshot" in names
    assert "web.fetch" in names
    assert "file.read" in names
    assert "file.list" in names
    assert "calendar.upcoming" in names
    assert "email.recent" in names
    assert all(spec.read_only for spec in specs)


def test_time_and_system_tools(tools) -> None:
    _settings, registry = tools
    now = registry.execute("time.now")
    assert now.ok
    assert "UTC:" in now.text

    system = registry.execute("system.snapshot")
    assert system.ok
    assert "CPU:" in system.text
    assert "Memory:" in system.text


def test_calendar_and_email_are_clear_when_empty(tools) -> None:
    _settings, registry = tools
    calendar = registry.execute("calendar.upcoming")
    assert calendar.ok
    assert "No upcoming events" in calendar.text

    email = registry.execute("email.recent")
    assert not email.ok
    assert "disabled" in email.text


def test_web_tool_rejects_other_domains(tools) -> None:
    _settings, registry = tools
    result = registry.execute("web.fetch", {"url": "https://example.com/"})
    assert not result.ok
    assert "not allowed" in result.text


def test_public_web_option_still_blocks_localhost(config_path: Path) -> None:
    settings = load_settings(config_path)
    assert settings.tools.web_allow_public is False

    from alice.app.tools import WebFetchTool, _public_host

    assert not _public_host("localhost")
    assert not _public_host("127.0.0.1")
    assert not _public_host("192.168.1.1")
    tool = WebFetchTool(settings.tools)
    assert not tool._allowed("localhost")


def test_file_list_stays_inside_roots(tools, tmp_path: Path) -> None:
    settings, registry = tools
    root = settings.inputs.files.roots[0]
    allowed = registry.execute("file.list", {"path": str(root), "limit": 3})
    assert allowed.ok

    outside = registry.execute("file.list", {"path": str(tmp_path.parent.parent)})
    if tmp_path.resolve().is_relative_to(root):
        pytest.skip("the temporary directory is inside the configured root")
    assert not outside.ok


def test_tool_calls_are_audited(tools) -> None:
    settings, registry = tools
    registry.execute("time.now")
    connection = connect(settings.db_path, create=False)
    try:
        row = connection.execute(
            "SELECT tool, ok, summary FROM tool_calls ORDER BY id DESC LIMIT 1"
        ).fetchone()
    finally:
        connection.close()
    assert row["tool"] == "time.now"
    assert row["ok"] == 1
    assert row["summary"]


def test_system_snapshot_shape() -> None:
    data = snapshot()
    assert data["cpu_count"] >= 1
    assert 0 <= float(data["cpu_percent"]) <= 100
    assert float(data["memory_total_gb"]) > 0
