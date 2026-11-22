"""Sensor interface (Tier 0). Sensors never call a model."""

from __future__ import annotations

from abc import ABC, abstractmethod

from ...core.events import RawEvent


class Sensor(ABC):
    """A read-only source of raw observations.

    `poll` returns newly observed events without persisting progress.
    `commit` stores progress once the caller has durably handled them, so a
    crash between the two re-reads a little (dedupe removes repeats) instead
    of losing observations.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Stable label for logs, e.g. `logfile:app.log`."""

    def start(self) -> None:
        """Prepare the sensor. Poll-driven sensors do nothing here."""

    def stop(self) -> None:
        """Release resources."""

    @abstractmethod
    def poll(self) -> list[RawEvent]:
        """Return observations since the last committed position."""

    def commit(self) -> None:
        """Persist progress after the last poll was handled."""

    def rollback(self) -> None:
        """Discard progress from a poll whose results were not handled."""
