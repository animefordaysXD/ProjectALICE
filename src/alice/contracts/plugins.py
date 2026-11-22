"""Plugin manifest and host contract.

Plugins are trusted, in-process code. Capabilities are declared up front so the
host can refuse one it does not implement.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

CAPABILITIES = ("sensor", "notifier", "job")


@dataclass(frozen=True)
class PluginManifest:
    id: str
    version: str
    capabilities: frozenset[str] = frozenset()
    description: str = ""

    def __post_init__(self) -> None:
        if not self.id or any(ch.isspace() for ch in self.id):
            raise ValueError("plugin id must be a non-empty token without spaces")
        unknown = self.capabilities - set(CAPABILITIES)
        if unknown:
            raise ValueError(f"unsupported plugin capabilities: {sorted(unknown)}")


class PluginContext(Protocol):
    """The narrow surface a plugin receives from the host."""

    def log(self, message: str) -> None: ...


class Plugin(Protocol):
    manifest: PluginManifest

    def start(self, context: PluginContext) -> None: ...

    def stop(self) -> None: ...
