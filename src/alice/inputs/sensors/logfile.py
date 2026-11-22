"""Rotation-safe log tailer (Tier 0, no AI)."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from ..config import Source
from ...core.db import utc_now
from ...core.events import CursorStore, RawEvent
from .base import Sensor
from .common import (
    DEFAULT_MAX_LINES,
    HEAD_BYTES,
    MAX_BYTES_PER_POLL,
    MAX_LINE_CHARS,
    encode_cursor,
    file_head,
    resolve_path,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _FileState:
    offset: int
    head: str
    size: int


def _decode_state(raw: str | None) -> _FileState | None:
    if raw is None:
        return None
    try:
        data = json.loads(raw)
        return _FileState(
            offset=int(data["offset"]),
            head=str(data.get("head", "")),
            size=int(data.get("size", 0)),
        )
    except (ValueError, TypeError, KeyError):
        logger.warning("unreadable log cursor; restarting this file from the beginning")
        return _FileState(offset=0, head="", size=0)


class LogfileSensor(Sensor):
    """Tails files matching a glob, surviving rotation and truncation.

    A file seen for the first time is tailed from its end unless the source
    sets `from_start: true`, so installing Alice does not replay a large
    existing log.
    """

    def __init__(self, source: Source, *, root: Path, cursors: CursorStore) -> None:
        options = dict(source.options)
        self.path = resolve_path(str(options["path"]), root)
        self.glob = str(options.get("glob", "*.log"))
        self.encoding = str(options.get("encoding", "utf-8"))
        self.from_start = bool(options.get("from_start", False))
        self.max_lines = int(options.get("max_lines", DEFAULT_MAX_LINES))
        self.cursors = cursors
        self._pending: dict[str, _FileState] = {}

    @property
    def name(self) -> str:
        return f"logfile:{self.path}"

    def poll(self) -> list[RawEvent]:
        events: list[RawEvent] = []
        for path in self._files():
            remaining = self.max_lines - len(events)
            if remaining <= 0:
                break
            events.extend(self._poll_file(path, limit=remaining))
        return events

    def commit(self) -> None:
        for source, state in self._pending.items():
            self.cursors.set(
                source,
                encode_cursor(
                    {"offset": state.offset, "head": state.head, "size": state.size}
                ),
            )
        self._pending.clear()

    def rollback(self) -> None:
        self._pending.clear()

    def _files(self) -> list[Path]:
        if self.path.is_file():
            return [self.path]
        if not self.path.is_dir():
            logger.debug("log directory not available yet: %s", self.path)
            return []
        try:
            return sorted(path for path in self.path.glob(self.glob) if path.is_file())
        except OSError as exc:
            logger.warning("cannot list log directory %s: %s", self.path, exc)
            return []

    def _poll_file(self, path: Path, *, limit: int) -> list[RawEvent]:
        cursor_key = f"logfile:{path.resolve().as_posix()}"
        state = self._pending.get(cursor_key) or _decode_state(
            self.cursors.get(cursor_key)
        )
        try:
            size = path.stat().st_size
        except OSError as exc:
            logger.warning("cannot stat %s: %s", path, exc)
            return []
        head = file_head(path, min(HEAD_BYTES, size))

        if state is None:
            state = _FileState(
                offset=0 if self.from_start else size, head=head, size=size
            )
        else:
            compare_len = min(HEAD_BYTES, state.size)
            replaced = compare_len > 0 and file_head(path, compare_len) != state.head
            if state.offset > size or (state.offset > 0 and replaced):
                logger.info("log rotated or rewritten: %s", path.name)
                state = _FileState(offset=0, head=head, size=size)

        if state.offset >= size:
            self._pending[cursor_key] = _FileState(state.offset, head, size)
            return []

        try:
            with path.open("rb") as handle:
                handle.seek(state.offset)
                chunk = handle.read(MAX_BYTES_PER_POLL)
        except OSError as exc:
            logger.warning("cannot read %s: %s", path, exc)
            return []

        parts = chunk.split(b"\n")
        consumed = 0
        events: list[RawEvent] = []
        if len(parts) == 1 and len(chunk) == MAX_BYTES_PER_POLL:
            # A line longer than the poll budget would otherwise stall the
            # cursor forever. Record a clipped event and move past it.
            logger.warning("log line exceeds poll budget: %s", path.name)
            text = chunk.decode(self.encoding, errors="replace").rstrip("\r")
            events.append(
                RawEvent(
                    source=f"logfile:{path.name}",
                    kind="log",
                    text=self._clip(text, path),
                    ts=utc_now(),
                    meta={"file": str(path), "offset": state.offset},
                )
            )
            consumed = len(chunk)
        else:
            for index, raw_line in enumerate(parts[:-1]):
                if index >= limit:
                    break
                consumed += len(raw_line) + 1
                text = raw_line.decode(self.encoding, errors="replace").rstrip("\r")
                if state.offset == 0 and index == 0:
                    text = text.lstrip("\ufeff")
                if not text.strip():
                    continue
                events.append(
                    RawEvent(
                        source=f"logfile:{path.name}",
                        kind="log",
                        text=self._clip(text, path),
                        ts=utc_now(),
                        meta={"file": str(path), "offset": state.offset},
                    )
                )

        self._pending[cursor_key] = _FileState(
            offset=state.offset + consumed, head=head, size=size
        )
        return events

    def _clip(self, text: str, path: Path) -> str:
        if len(text) <= MAX_LINE_CHARS:
            return text
        logger.warning("log line truncated: %s chars=%d", path.name, len(text))
        return text[:MAX_LINE_CHARS] + " …[truncated]"
