"""Install and inspect the logon Scheduled Task.

Windows Task Scheduler is used instead of a service so Alice runs in the user's
interactive session (needed for toasts) and keeps access to per-user DPAPI
secrets and the user's local data directory. The task is hidden, starts at
logon, and asks Task Scheduler to restart it a few times if it exits badly.
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

TASK_NAME = "Alice"


class StartupError(RuntimeError):
    """The Scheduled Task could not be installed or inspected."""


@dataclass(frozen=True)
class StartupStatus:
    installed: bool
    state: str
    task: str = TASK_NAME


def _ps_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def build_install_script(
    exe_path: Path | str,
    *,
    config_path: Path | str | None = None,
    user: str | None = None,
) -> str:
    """PowerShell that registers a hidden, restart-on-failure logon task."""
    exe = str(Path(exe_path))
    inner = f"& {_ps_quote(exe)} run"
    if config_path is not None:
        inner += f" --config {_ps_quote(str(config_path))}"
    argument = (
        "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass "
        f'-Command "{inner}"'
    )
    working = str(Path(exe).parent)
    trigger = "New-ScheduledTaskTrigger -AtLogOn"
    if user:
        trigger += f" -User {_ps_quote(user)}"
    return (
        "$ErrorActionPreference = 'Stop'\n"
        f"$action = New-ScheduledTaskAction -Execute 'powershell.exe' "
        f"-Argument {_ps_quote(argument)} -WorkingDirectory {_ps_quote(working)}\n"
        f"$trigger = {trigger}\n"
        "$settings = New-ScheduledTaskSettingsSet -Hidden "
        "-AllowStartIfOnBatteries -DontStopIfGoingOnBatteries "
        "-RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) "
        "-ExecutionTimeLimit ([TimeSpan]::Zero)\n"
        f"Register-ScheduledTask -TaskName {_ps_quote(TASK_NAME)} "
        "-Action $action -Trigger $trigger -Settings $settings -Force | Out-Null\n"
        f"Get-ScheduledTask -TaskName {_ps_quote(TASK_NAME)} | "
        "Select-Object -ExpandProperty TaskName\n"
    )


def build_remove_script() -> str:
    return (
        "$ErrorActionPreference = 'Stop'\n"
        f"Unregister-ScheduledTask -TaskName {_ps_quote(TASK_NAME)} "
        "-Confirm:$false\n"
    )


def build_status_script() -> str:
    return (
        "$ErrorActionPreference = 'Stop'\n"
        f"$task = Get-ScheduledTask -TaskName {_ps_quote(TASK_NAME)} "
        "-ErrorAction SilentlyContinue\n"
        "if ($null -eq $task) { 'missing'; exit 0 }\n"
        "$task.State\n"
    )


def _powershell(
    script: str, *, runner=subprocess.run
) -> subprocess.CompletedProcess[str]:
    if sys.platform != "win32":
        raise StartupError("startup integration uses Windows Task Scheduler")
    try:
        return runner(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                script,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise StartupError(f"cannot run powershell.exe: {exc}") from exc


def install_startup(
    exe_path: Path | str,
    *,
    config_path: Path | str | None = None,
    user: str | None = None,
    runner=subprocess.run,
) -> StartupStatus:
    result = _powershell(
        build_install_script(exe_path, config_path=config_path, user=user),
        runner=runner,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise StartupError(f"could not install the logon task: {detail}")
    return StartupStatus(installed=True, state="Ready")


def remove_startup(*, runner=subprocess.run) -> StartupStatus:
    result = _powershell(build_remove_script(), runner=runner)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise StartupError(f"could not remove the logon task: {detail}")
    return StartupStatus(installed=False, state="missing")


def startup_status(*, runner=subprocess.run) -> StartupStatus:
    result = _powershell(build_status_script(), runner=runner)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise StartupError(f"could not inspect the logon task: {detail}")
    state = (result.stdout or "").strip() or "unknown"
    return StartupStatus(installed=state.lower() != "missing", state=state)
