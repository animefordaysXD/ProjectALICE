"""Local message stub (Tier 0, no AI).

Reads a text file, one message per line, until the first real messaging
adapter is chosen (see DECISIONS.md, OPEN #1).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from ..config import Source
from ...core.db import utc_now
from ...core.events import CursorStore, RawEvent
from .base import Sensor
from .common import HEAD_BYTES, DEFAULT_MAX_LINES, encode_cursor, file_head, resolve_path

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _LineState:
    line: int
    head: str
    size: int


class StubMessageSensor(Sensor):
    def __init__(
        self,
        source: Source,
        *,
        root: Path,
        cursors: CursorStore,
        from_start_override: bool | None = None,
        max_lines_override: int | None = None,
    ) -> None:
        options = dict(source.options)
        self.path = resolve_path(str(options["path"]), root)
        self.encoding = str(options.get("encoding", "utf-8"))
        self.from_start = (
            bool(from_start_override)
            if from_start_override is not None
            else bool(options.get("from_start", True))
        )
        self.max_lines = (
            int(max_lines_override)
            if max_lines_override is not None
            else int(options.get("max_lines", DEFAULT_MAX_LINES))
        )
        self.cursors = cursors
        self._pending: dict[str, _LineState] = {}

    @property
    def name(self) -> str:
        return f"message:stub:{self.path}"

    def poll(self) -> list[RawEvent]:
        cursor_key = f"message:stub:{self.path.resolve().as_posix()}"
        state = self._pending.get(cursor_key) or self._load_state(cursor_key)

        if not self.path.is_file():
            logger.debug("message stub file not available yet: %s", self.path)
            return []

        try:
            size = self.path.stat().st_size
        except OSError as exc:
            logger.warning("cannot stat %s: %s", self.path, exc)
            return []
        head = file_head(self.path, min(HEAD_BYTES, size))

        if state is None:
            skip = 0 if self.from_start else self._count_lines()
        else:
            skip = state.line
            compare_len = min(HEAD_BYTES, state.size)
            replaced = compare_len > 0 and file_head(self.path, compare_len) != state.head
            if size < state.size or replaced:
                logger.info("message stub replaced: %s", self.path.name)
                skip = 0

        events: list[RawEvent] = []
        consumed = skip
        try:
            with self.path.open("rb") as handle:
                for index, raw_line in enumerate(handle):
                    if index < skip:
                        continue
                    consumed = index + 1
                    text = raw_line.decode(self.encoding, errors="replace").rstrip("\r\n")
                    if index == 0:
                        text = text.lstrip("\ufeff")
                    if not text.strip():
                        continue
                    events.append(
                        RawEvent(
                            source="message:stub",
                            kind="message",
                            text=text,
                            ts=utc_now(),
                            meta={"file": str(self.path), "line": index + 1},
                        )
                    )
                    if len(events) >= self.max_lines:
                        break
        except OSError as exc:
            logger.warning("cannot read %s: %s", self.path, exc)
            return []

        self._pending[cursor_key] = _LineState(line=consumed, head=head, size=size)
        return events

    def commit(self) -> None:
        for source, state in self._pending.items():
            self.cursors.set(
                source,
                encode_cursor({"line": state.line, "head": state.head, "size": state.size}),
            )
        self._pending.clear()

    def rollback(self) -> None:
        self._pending.clear()

    def _load_state(self, cursor_key: str) -> _LineState | None:
        raw = self.cursors.get(cursor_key)
        if raw is None:
            return None
        try:
            data = json.loads(raw)
            return _LineState(
                line=int(data["line"]),
                head=str(data.get("head", "")),
                size=int(data.get("size", 0)),
            )
        except (ValueError, TypeError, KeyError):
            logger.warning("unreadable message cursor; reading from the start")
            return _LineState(line=0, head="", size=0)

    def _count_lines(self) -> int:
        try:
            with self.path.open("rb") as handle:
                return sum(1 for _ in handle)
        except OSError:
            return 0
