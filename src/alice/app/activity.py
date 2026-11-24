"""Local foreground-activity snapshot.

This is deliberately narrow: process name, window title, and idle seconds. No
keystrokes, screenshots, clipboard, browser history, or file contents.
"""

from __future__ import annotations

import ctypes
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

CATEGORY_APPS: dict[str, str] = {
    "code.exe": "coding",
    "code - insiders.exe": "coding",
    "cursor.exe": "coding",
    "devenv.exe": "coding",
    "pycharm64.exe": "coding",
    "idea64.exe": "coding",
    "webstorm64.exe": "coding",
    "sublime_text.exe": "coding",
    "notepad++.exe": "coding",
    "windowsterminal.exe": "terminal",
    "wt.exe": "terminal",
    "powershell.exe": "terminal",
    "pwsh.exe": "terminal",
    "cmd.exe": "terminal",
    "alacritty.exe": "terminal",
    "wezterm-gui.exe": "terminal",
    "chrome.exe": "browsing",
    "msedge.exe": "browsing",
    "firefox.exe": "browsing",
    "brave.exe": "browsing",
    "opera.exe": "browsing",
    "arc.exe": "browsing",
    "slack.exe": "chat",
    "teams.exe": "chat",
    "discord.exe": "chat",
    "zoom.exe": "meeting",
    "webexmta.exe": "meeting",
    "winword.exe": "writing",
    "notepad.exe": "writing",
    "obsidian.exe": "writing",
    "notion.exe": "writing",
    "typora.exe": "writing",
    "figma.exe": "design",
    "photoshop.exe": "design",
    "illustrator.exe": "design",
    "explorer.exe": "files",
    "spotify.exe": "media",
    "vlc.exe": "media",
}

FOCUS_CATEGORIES = {"coding", "terminal", "writing", "design", "browsing"}
TITLE_HINTS = (
    ("youtube", "media"),
    ("netflix", "media"),
    ("twitch", "media"),
    ("spotify", "media"),
    ("zoom meeting", "meeting"),
    ("google meet", "meeting"),
    ("teams meeting", "meeting"),
)


@dataclass(frozen=True)
class Activity:
    app: str
    title: str
    category: str
    idle_seconds: int
    captured_at: str

    def describe(self) -> str:
        app = self.app or "unknown app"
        label = self.category or "other"
        idle = _duration(self.idle_seconds) if self.idle_seconds >= 60 else "active"
        return f"{app} ({label}), {idle}"


def snapshot() -> Activity | None:
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        user32.GetForegroundWindow.restype = wintypes.HWND
        user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
        user32.GetWindowTextLengthW.restype = ctypes.c_int
        user32.GetWindowTextW.argtypes = [
            wintypes.HWND,
            wintypes.LPWSTR,
            ctypes.c_int,
        ]
        user32.GetWindowTextW.restype = ctypes.c_int
        user32.GetWindowThreadProcessId.argtypes = [
            wintypes.HWND,
            ctypes.POINTER(wintypes.DWORD),
        ]
        kernel32.OpenProcess.argtypes = [
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
        ]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD),
        ]
        kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        hwnd = user32.GetForegroundWindow()
        title = ""
        app = ""
        if hwnd:
            length = max(0, user32.GetWindowTextLengthW(hwnd))
            buffer = ctypes.create_unicode_buffer(length + 1)
            if user32.GetWindowTextW(hwnd, buffer, length + 1):
                title = buffer.value.strip()[:300]
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value:
                handle = kernel32.OpenProcess(
                    PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value
                )
                if handle:
                    try:
                        path_buffer = ctypes.create_unicode_buffer(1024)
                        size = wintypes.DWORD(len(path_buffer))
                        if kernel32.QueryFullProcessImageNameW(
                            handle, 0, path_buffer, ctypes.byref(size)
                        ):
                            app = Path(path_buffer.value).name
                    finally:
                        kernel32.CloseHandle(handle)

        idle = _idle_seconds(user32, kernel32, wintypes, ctypes)
        if not hwnd:
            return Activity(
                app="",
                title="",
                category="idle" if idle >= 60 else "unknown",
                idle_seconds=idle,
                captured_at=datetime.now().astimezone().isoformat(timespec="seconds"),
            )
        return Activity(
            app=app,
            title=title,
            category=categorize(app, title),
            idle_seconds=idle,
            captured_at=datetime.now().astimezone().isoformat(timespec="seconds"),
        )
    except Exception:
        return None


def categorize(app: str, title: str) -> str:
    lowered = f"{app} {title}".casefold()
    for hint, category in TITLE_HINTS:
        if hint in lowered:
            return category
    return CATEGORY_APPS.get(app.casefold(), "other")


def _idle_seconds(user32, kernel32, wintypes, ctypes) -> int:
    class LASTINPUTINFO(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.UINT),
            ("dwTime", wintypes.DWORD),
        ]

    info = LASTINPUTINFO()
    info.cbSize = ctypes.sizeof(LASTINPUTINFO)
    kernel32.GetTickCount.restype = wintypes.DWORD
    user32.GetLastInputInfo.argtypes = [ctypes.POINTER(LASTINPUTINFO)]
    user32.GetLastInputInfo.restype = wintypes.BOOL
    if not user32.GetLastInputInfo(ctypes.byref(info)):
        return 0
    tick = int(kernel32.GetTickCount())
    elapsed = (tick - int(info.dwTime)) & 0xFFFFFFFF
    return max(0, elapsed // 1000)


def redact_activity(activity: Activity, clean) -> Activity:
    return Activity(
        app=activity.app,
        title=clean(activity.title)[:300],
        category=activity.category,
        idle_seconds=activity.idle_seconds,
        captured_at=activity.captured_at,
    )


def _duration(seconds: int) -> str:
    minutes = seconds // 60
    if minutes < 60:
        return f"idle {minutes}m"
    return f"idle {minutes // 60}h {minutes % 60:02d}m"
