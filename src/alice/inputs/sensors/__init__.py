"""Sensor construction and the poll/ingest loop used by `alice run`."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from pathlib import Path
from typing import Iterable

from ...core.events import CursorStore, WriteKind
from ..config import Source
from ..gate import Gate, IngestResult
from .base import Sensor
from .logfile import LogfileSensor
from .logscan import LogScanSensor
from .message_stub import StubMessageSensor

logger = logging.getLogger(__name__)


def build_sensors(
    sources: Iterable[Source],
    *,
    root: Path,
    cursors: CursorStore,
    exclude_paths: Iterable[Path | str] = (),
    from_start: bool | None = None,
    max_lines: int | None = None,
) -> list[Sensor]:
    """Create one sensor per configured source."""
    sensors: list[Sensor] = []
    for source in sources:
        if source.type == "logfile":
            sensors.append(
                LogfileSensor(
                    source,
                    root=root,
                    cursors=cursors,
                    from_start_override=from_start,
                    max_lines_override=max_lines,
                )
            )
        elif source.type == "logscan":
            sensors.append(
                LogScanSensor(
                    source,
                    root=root,
                    cursors=cursors,
                    exclude_paths=exclude_paths,
                    from_start_override=from_start,
                    max_lines_override=max_lines,
                )
            )
        elif source.type == "message":
            sensors.append(
                StubMessageSensor(
                    source,
                    root=root,
                    cursors=cursors,
                    from_start_override=from_start,
                    max_lines_override=max_lines,
                )
            )
        else:  # config validation should make this unreachable
            raise ValueError(f"unsupported source type: {source.type}")
    return sensors


@dataclass
class PollStats:
    polls: int = 0
    observed: int = 0
    inserted: int = 0
    collapsed: int = 0
    duplicates: int = 0
    above_threshold: int = 0
    redacted: int = 0
    failures: int = 0
    results: list[IngestResult] = field(default_factory=list)

    @property
    def total(self) -> int:
        return self.inserted + self.collapsed + self.duplicates


def poll_once(sensors: list[Sensor], gate: Gate) -> PollStats:
    """Poll every sensor, ingest what it returns, then commit progress.

    A failing sensor is logged and skipped; its cursor is not advanced, so
    the next poll retries it.
    """
    stats = PollStats()
    for sensor in sensors:
        stats.polls += 1
        try:
            raw_events = sensor.poll()
            results = gate.ingest_many(raw_events)
        except Exception:
            stats.failures += 1
            sensor.rollback()
            logger.exception("sensor failed: %s", sensor.name)
            continue

        sensor.commit()
        stats.observed += len(raw_events)
        stats.results.extend(results)
        for result in results:
            stats.redacted += 1 if result.redactions else 0
            if result.write.kind == WriteKind.INSERTED:
                stats.inserted += 1
            elif result.write.kind == WriteKind.COLLAPSED:
                stats.collapsed += 1
            else:
                stats.duplicates += 1
            if result.passes:
                stats.above_threshold += 1
    return stats
