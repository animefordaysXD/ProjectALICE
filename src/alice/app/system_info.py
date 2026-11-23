"""Small stdlib-only hardware and process snapshot."""

from __future__ import annotations

import ctypes
import os
import shutil
import sys
import time
from pathlib import Path


def snapshot() -> dict[str, float | int | str]:
    data: dict[str, float | int | str] = {
        "cpu_count": os.cpu_count() or 0,
        "cpu_percent": 0.0,
        "memory_used_gb": 0.0,
        "memory_total_gb": 0.0,
        "disk_used_gb": 0.0,
        "disk_total_gb": 0.0,
        "disk_free_gb": 0.0,
        "process_count": 0,
        "uptime_hours": 0.0,
        "platform": sys.platform,
    }
    if sys.platform == "win32":
        _windows(data)
    else:
        _portable(data)
    return data


def _windows(data: dict[str, float | int | str]) -> None:
    class FILETIME(ctypes.Structure):
        _fields_ = [
            ("dwLowDateTime", ctypes.c_ulong),
            ("dwHighDateTime", ctypes.c_ulong),
        ]

    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_ulong),
            ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    kernel32 = ctypes.windll.kernel32
    try:
        idle1 = FILETIME()
        kernel1 = FILETIME()
        user1 = FILETIME()
        kernel32.GetSystemTimes(
            ctypes.byref(idle1), ctypes.byref(kernel1), ctypes.byref(user1)
        )
        time.sleep(0.1)
        idle2 = FILETIME()
        kernel2 = FILETIME()
        user2 = FILETIME()
        kernel32.GetSystemTimes(
            ctypes.byref(idle2), ctypes.byref(kernel2), ctypes.byref(user2)
        )
        idle = _ft(idle2) - _ft(idle1)
        total = (_ft(kernel2) - _ft(kernel1)) + (_ft(user2) - _ft(user1))
        if total > 0:
            data["cpu_percent"] = round(max(0.0, min(100.0, 100.0 * (1.0 - idle / total))), 1)
    except Exception:
        pass

    try:
        memory = MEMORYSTATUSEX()
        memory.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        kernel32.GlobalMemoryStatusEx(ctypes.byref(memory))
        total = memory.ullTotalPhys / (1024**3)
        used = (memory.ullTotalPhys - memory.ullAvailPhys) / (1024**3)
        data["memory_total_gb"] = round(total, 1)
        data["memory_used_gb"] = round(used, 1)
    except Exception:
        pass

    try:
        data["uptime_hours"] = round(kernel32.GetTickCount64() / 3_600_000, 1)
    except Exception:
        pass

    try:
        psapi = ctypes.windll.psapi
        count = 2048
        while True:
            array = (ctypes.c_ulong * count)()
            needed = ctypes.c_ulong()
            if psapi.EnumProcesses(
                ctypes.byref(array),
                ctypes.sizeof(array),
                ctypes.byref(needed),
            ):
                data["process_count"] = needed.value // ctypes.sizeof(ctypes.c_ulong)
                break
            count *= 2
            if count > 65536:
                break
    except Exception:
        pass

    try:
        anchor = Path.home().anchor or "C:\\"
        usage = shutil.disk_usage(anchor)
        data["disk_total_gb"] = round(usage.total / (1024**3), 1)
        data["disk_used_gb"] = round(usage.used / (1024**3), 1)
        data["disk_free_gb"] = round(usage.free / (1024**3), 1)
    except OSError:
        pass


def _portable(data: dict[str, float | int | str]) -> None:
    try:
        usage = shutil.disk_usage(str(Path.home()))
        data["disk_total_gb"] = round(usage.total / (1024**3), 1)
        data["disk_used_gb"] = round(usage.used / (1024**3), 1)
        data["disk_free_gb"] = round(usage.free / (1024**3), 1)
    except OSError:
        pass
    try:
        import psutil  # type: ignore

        data["cpu_percent"] = round(psutil.cpu_percent(interval=0.1), 1)
        memory = psutil.virtual_memory()
        data["memory_total_gb"] = round(memory.total / (1024**3), 1)
        data["memory_used_gb"] = round(memory.used / (1024**3), 1)
        data["process_count"] = len(psutil.pids())
        data["uptime_hours"] = round(
            (time.time() - psutil.boot_time()) / 3600, 1
        )
    except Exception:
        pass


def _ft(value: object) -> int:
    return (value.dwHighDateTime << 32) | value.dwLowDateTime  # type: ignore[attr-defined]
