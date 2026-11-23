"""Read-only file access for chat attachments.

Alice may read a text file only when the user explicitly attaches it. The file
must resolve under one of the configured roots, fit the byte cap, and look like
text. Content is redacted before it reaches a model or the local transcript.
There is no write path in this module.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable

from .config import FilesConfig

logger = logging.getLogger(__name__)


class FileAccessError(RuntimeError):
    """The file cannot be read safely under the current configuration."""


@dataclass(frozen=True)
class FileText:
    path: Path
    text: str
    size: int
    truncated: bool


class FileReader:
    def __init__(
        self,
        config: FilesConfig,
        *,
        sanitize: Callable[[str], str] | None = None,
    ) -> None:
        self.config = config
        self.sanitize = sanitize
        self.roots = tuple(root.expanduser().resolve() for root in config.roots)

    def read(self, path: Path | str, *, max_bytes: int | None = None) -> FileText:
        candidate = Path(path).expanduser()
        try:
            resolved = candidate.resolve()
        except OSError as exc:
            raise FileAccessError(f"cannot resolve {candidate}: {exc}") from exc
        if not self._allowed(resolved):
            raise FileAccessError(
                f"{resolved} is outside files.roots; edit the config to allow it"
            )
        if not resolved.is_file():
            raise FileAccessError(f"not a file: {resolved}")

        try:
            size = resolved.stat().st_size
        except OSError as exc:
            raise FileAccessError(f"cannot stat {resolved}: {exc}") from exc
        limit = (
            self.config.max_bytes
            if max_bytes is None
            else min(int(max_bytes), self.config.max_bytes)
        )
        limit = max(1, limit)
        try:
            with resolved.open("rb") as handle:
                payload = handle.read(limit)
        except OSError as exc:
            raise FileAccessError(f"cannot read {resolved}: {exc}") from exc
        if b"\x00" in payload[:2048]:
            raise FileAccessError(f"{resolved} looks binary; only text files are supported")

        text = _decode(payload)
        if self.sanitize is not None:
            text = self.sanitize(text)
        return FileText(
            path=resolved,
            text=text,
            size=size,
            truncated=size > len(payload),
        )

    def list_dir(
        self, path: Path | str, *, limit: int = 100
    ) -> tuple[tuple[str, int, str], ...]:
        """List entries under `files.roots` without reading file contents."""
        candidate = Path(path).expanduser()
        try:
            resolved = candidate.resolve()
        except OSError as exc:
            raise FileAccessError(f"cannot resolve {candidate}: {exc}") from exc
        if not self._allowed(resolved):
            raise FileAccessError(
                f"{resolved} is outside files.roots; edit the config to allow it"
            )
        if not resolved.is_dir():
            raise FileAccessError(f"not a directory: {resolved}")
        rows: list[tuple[str, int, str]] = []
        try:
            entries = sorted(
                resolved.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower())
            )
        except OSError as exc:
            raise FileAccessError(f"cannot list {resolved}: {exc}") from exc
        for entry in entries[: max(1, min(limit, 500))]:
            try:
                stat = entry.stat()
                size = 0 if entry.is_dir() else stat.st_size
                modified = datetime.fromtimestamp(
                    stat.st_mtime, tz=timezone.utc
                ).strftime("%Y-%m-%d %H:%M")
            except OSError:
                size, modified = 0, "?"
            rows.append((entry.name + ("/" if entry.is_dir() else ""), size, modified))
        return tuple(rows)

    def read_many(
        self, paths: Iterable[Path | str], *, max_files: int | None = None
    ) -> tuple[FileText, ...]:
        limit = self.config.max_files if max_files is None else max_files
        files = list(paths)
        if len(files) > limit:
            raise FileAccessError(
                f"attach at most {limit} files per chat question"
            )
        return tuple(self.read(path) for path in files)

    def _allowed(self, path: Path) -> bool:
        for root in self.roots:
            if path == root or root in path.parents:
                return True
        return False


def _decode(payload: bytes) -> str:
    if payload.startswith(b"\xff\xfe") or payload.startswith(b"\xfe\xff"):
        return payload.decode("utf-16", errors="replace")
    return payload.decode("utf-8-sig", errors="replace")
