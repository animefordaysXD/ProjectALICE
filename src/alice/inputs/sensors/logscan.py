"""Bounded whole-drive log discovery.

A normal `logfile` source watches one directory. This source walks a larger
tree (for example `C:/`), caches the matching files, and refreshes that list on
an interval. Exclusions, a depth limit, a file cap, and an age limit keep the
scan from turning into an unbounded crawl of Windows and Program Files.
"""

from __future__ import annotations

import fnmatch
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Sequence

from ..config import Source
from ...core.events import CursorStore
from .logfile import LogfileSensor

logger = logging.getLogger(__name__)

DEFAULT_GLOBS: tuple[str, ...] = ("*.log", "*.txt", "*.jsonl")
DEFAULT_MAX_FILES = 5000
DEFAULT_REFRESH_MINUTES = 30
DEFAULT_MAX_DEPTH = 20
DEFAULT_RECENT_DAYS = 14
DEFAULT_TEXT_ONLY = True

# Names are matched case-insensitively at any depth. These are system or build
# folders that are either inaccessible, enormous, or almost never contain the
# application logs the user wants.
DEFAULT_EXCLUDE_DIRS: frozenset[str] = frozenset(
    {
        "$recycle.bin",
        "$sysreset",
        "$winreagent",
        "system volume information",
        "recovery",
        "perflogs",
        "msocache",
        "config.msi",
        "documents and settings",
        "windows",
        "program files",
        "program files (x86)",
        "programdata",
        ".git",
        "node_modules",
        "__pycache__",
        ".venv",
        "venv",
        "site-packages",
        "build",
        "dist",
        "obj",
    }
)


@dataclass(frozen=True)
class ScanResult:
    files: tuple[Path, ...]
    dirs_seen: int
    errors: int
    truncated: bool


def coerce_globs(value: object) -> tuple[str, ...]:
    """Accept a comma string, a single glob, or a list of globs."""
    if value is None:
        return DEFAULT_GLOBS
    if isinstance(value, str):
        items = [part.strip() for part in value.split(",") if part.strip()]
    elif isinstance(value, (list, tuple)):
        items = [str(part).strip() for part in value if str(part).strip()]
    else:
        return DEFAULT_GLOBS
    return tuple(items) or DEFAULT_GLOBS


def _normalize_path(value: Path | str) -> str:
    return os.path.normcase(os.path.abspath(str(value)))


def _looks_textual(path: Path) -> bool:
    """Reject caches and databases that merely happen to end in .log/.txt."""
    try:
        with path.open("rb") as handle:
            head = handle.read(512)
    except OSError:
        return False
    return b"\x00" not in head


def _excluded(
    path: Path,
    name: str,
    exclude_names: frozenset[str],
    exclude_prefixes: tuple[str, ...],
) -> bool:
    if name.casefold() in exclude_names:
        return True
    if not exclude_prefixes:
        return False
    candidate = _normalize_path(path)
    for prefix in exclude_prefixes:
        if candidate == prefix or candidate.startswith(prefix + os.sep):
            return True
    return False


def discover_log_files(
    root: Path | str,
    *,
    globs: Sequence[str] = DEFAULT_GLOBS,
    exclude_dirs: Iterable[str] = DEFAULT_EXCLUDE_DIRS,
    exclude_paths: Iterable[Path | str] = (),
    max_files: int = DEFAULT_MAX_FILES,
    max_depth: int = DEFAULT_MAX_DEPTH,
    recent_days: int = DEFAULT_RECENT_DAYS,
    text_only: bool = DEFAULT_TEXT_ONLY,
    now: float | None = None,
    progress: Callable[[int, int, int], None] | None = None,
) -> ScanResult:
    """Find matching files without following links or entering excluded trees."""
    # Resolve the root once so scandir yields long paths, matching the
    # resolved exclude prefixes even when %TEMP% was given as ADMINC~1.
    start = Path(root).expanduser().resolve()
    patterns = tuple(pattern.casefold() for pattern in globs) or DEFAULT_GLOBS
    exclude_names = frozenset(name.casefold() for name in exclude_dirs)
    exclude_prefixes = tuple(
        _normalize_path(Path(path).expanduser().resolve()) for path in exclude_paths
    )
    cutoff = (
        (time.time() if now is None else now) - recent_days * 86400
        if recent_days > 0
        else None
    )

    files: list[Path] = []
    directories = 0
    errors = 0
    truncated = False

    def matches(name: str) -> bool:
        lowered = name.casefold()
        return any(fnmatch.fnmatchcase(lowered, pattern) for pattern in patterns)

    def walk(directory: Path, depth: int) -> None:
        nonlocal directories, errors, truncated
        directories += 1
        if directories % 1000 == 0:
            logger.info(
                "logscan: %d directories, %d files, %d errors",
                directories,
                len(files),
                errors,
            )
            if progress is not None:
                progress(directories, len(files), errors)
        try:
            with os.scandir(directory) as iterator:
                entries = sorted(iterator, key=lambda item: item.name.casefold())
        except OSError:
            errors += 1
            return

        for entry in entries:
            if len(files) >= max_files:
                truncated = True
                return
            try:
                if entry.is_symlink():
                    continue
                path = Path(entry.path)
                if entry.is_dir(follow_symlinks=False):
                    if depth >= max_depth:
                        continue
                    if _excluded(path, entry.name, exclude_names, exclude_prefixes):
                        continue
                    walk(path, depth + 1)
                elif entry.is_file(follow_symlinks=False) and matches(entry.name):
                    if cutoff is not None:
                        try:
                            if entry.stat(follow_symlinks=False).st_mtime < cutoff:
                                continue
                        except OSError:
                            errors += 1
                            continue
                    if text_only and not _looks_textual(path):
                        continue
                    files.append(path)
            except OSError:
                errors += 1

    if start.is_file():
        if matches(start.name):
            files.append(start)
    elif start.is_dir():
        walk(start, 0)
    else:
        logger.warning("logscan root is not available: %s", start)
        errors += 1

    if truncated:
        logger.warning(
            "logscan stopped at the %d-file cap under %s; raise max_files or "
            "narrow the root",
            max_files,
            start,
        )
    logger.info(
        "logscan found %d files in %d directories (%d errors) under %s",
        len(files),
        directories,
        errors,
        start,
    )
    return ScanResult(
        files=tuple(files), dirs_seen=directories, errors=errors, truncated=truncated
    )


class LogScanSensor(LogfileSensor):
    """A logfile sensor with cached recursive discovery."""

    def __init__(
        self,
        source: Source,
        *,
        root: Path,
        cursors: CursorStore,
        exclude_paths: Iterable[Path | str] = (),
        from_start_override: bool | None = None,
        max_lines_override: int | None = None,
    ) -> None:
        super().__init__(
            source,
            root=root,
            cursors=cursors,
            from_start_override=from_start_override,
            max_lines_override=max_lines_override,
        )
        self.path = self.path.resolve()
        options = dict(source.options)
        self.globs = coerce_globs(options.get("globs", options.get("glob")))
        self.exclude_dirs = frozenset(
            str(name).casefold()
            for name in options.get("exclude_dirs", DEFAULT_EXCLUDE_DIRS)
        )
        configured = options.get("exclude_paths", ())
        if isinstance(configured, (str, Path)):
            configured = (configured,)
        self.exclude_paths = tuple(configured) + tuple(exclude_paths)
        self.max_files = max(1, int(options.get("max_files", DEFAULT_MAX_FILES)))
        self.refresh_seconds = max(
            60, int(options.get("refresh_minutes", DEFAULT_REFRESH_MINUTES)) * 60
        )
        self.max_depth = max(1, int(options.get("max_depth", DEFAULT_MAX_DEPTH)))
        self.recent_days = max(0, int(options.get("recent_days", DEFAULT_RECENT_DAYS)))
        self.text_only = bool(options.get("text_only", DEFAULT_TEXT_ONLY))
        self._cache: list[Path] | None = None
        self._cache_at = 0.0
        self._rotation = 0

    @property
    def name(self) -> str:
        return f"logscan:{self.path}"

    def _source_label(self, path: Path) -> str:
        try:
            relative = path.relative_to(self.path)
        except ValueError:
            relative = Path(path.name)
        label = relative.as_posix()
        if label in ("", "."):
            label = path.name
        return f"logfile:{label}"

    def _files(self) -> list[Path]:
        now = time.monotonic()
        if self._cache is None or now - self._cache_at >= self.refresh_seconds:
            result = discover_log_files(
                self.path,
                globs=self.globs,
                exclude_dirs=self.exclude_dirs,
                exclude_paths=self.exclude_paths,
                max_files=self.max_files,
                max_depth=self.max_depth,
                recent_days=self.recent_days,
                text_only=self.text_only,
            )
            self._cache = list(result.files)
            self._cache_at = now
            self._rotation = 0

        files = self._cache
        if not files:
            return []
        # Start at a different file each poll so a noisy early file cannot
        # starve the rest of the drive.
        start = self._rotation % len(files)
        self._rotation = (start + 1) % len(files)
        return files[start:] + files[:start]
