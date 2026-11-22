from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from alice.core.startup import (
    StartupError,
    build_install_script,
    build_remove_script,
    build_status_script,
    install_startup,
    remove_startup,
    startup_status,
)

WINDOWS_ONLY = pytest.mark.skipif(
    sys.platform != "win32", reason="Task Scheduler is Windows-only"
)


class FakeRunner:
    def __init__(self, *, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.calls: list[list[str]] = []
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr

    def __call__(self, args, **_kwargs):
        self.calls.append(list(args))
        return subprocess.CompletedProcess(
            args, self.returncode, stdout=self.stdout, stderr=self.stderr
        )


def test_install_script_is_hidden_and_restarts(tmp_path: Path) -> None:
    exe = tmp_path / "Alice" / "alice.exe"
    config = tmp_path / "config" / "alice.yaml"

    script = build_install_script(exe, config_path=config, user="Jo")

    assert "Register-ScheduledTask -TaskName 'Alice'" in script
    assert "-WindowStyle Hidden" in script
    assert "-RestartCount 3" in script
    assert str(exe) in script
    assert str(config) in script
    assert "-AtLogOn -User 'Jo'" in script
    assert "powershell.exe" in script


def test_install_script_escapes_apostrophes(tmp_path: Path) -> None:
    exe = tmp_path / "Jo's tools" / "alice.exe"

    script = build_install_script(exe)

    assert "Jo''s tools" in script


def test_remove_and_status_scripts() -> None:
    assert "Unregister-ScheduledTask -TaskName 'Alice'" in build_remove_script()
    assert "Get-ScheduledTask -TaskName 'Alice'" in build_status_script()


@WINDOWS_ONLY
def test_install_startup_invokes_powershell(tmp_path: Path) -> None:
    runner = FakeRunner(stdout="Alice\n")

    status = install_startup(tmp_path / "alice.exe", runner=runner)

    assert status.installed is True
    assert runner.calls[0][0] == "powershell.exe"
    assert any("Register-ScheduledTask" in arg for arg in runner.calls[0])


@WINDOWS_ONLY
def test_install_startup_reports_failure(tmp_path: Path) -> None:
    runner = FakeRunner(returncode=1, stderr="access denied")

    with pytest.raises(StartupError, match="access denied"):
        install_startup(tmp_path / "alice.exe", runner=runner)


@WINDOWS_ONLY
def test_status_and_remove(tmp_path: Path) -> None:
    ready = FakeRunner(stdout="Ready\n")
    status = startup_status(runner=ready)
    assert status.installed is True
    assert status.state == "Ready"

    missing = FakeRunner(stdout="missing\n")
    status = startup_status(runner=missing)
    assert status.installed is False

    removed = remove_startup(runner=FakeRunner())
    assert removed.installed is False
