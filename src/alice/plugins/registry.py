"""Explicit plugin registry.

Nothing is discovered from disk. `app` registers built-ins; a future opt-in
loader can add more. Startup failures are contained so one bad plugin cannot
stop Alice.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable

from ..contracts.plugins import Plugin, PluginContext, PluginManifest

logger = logging.getLogger(__name__)


class PluginError(RuntimeError):
    """A plugin is malformed, duplicated, or unsupported."""


class PluginRegistry:
    def __init__(self) -> None:
        self._plugins: dict[str, Plugin] = {}

    def register(self, plugin: Plugin) -> None:
        manifest = plugin.manifest
        if not isinstance(manifest, PluginManifest):
            raise PluginError("plugin.manifest must be a PluginManifest")
        if manifest.id in self._plugins:
            raise PluginError(f"duplicate plugin id: {manifest.id}")
        self._plugins[manifest.id] = plugin

    def get(self, plugin_id: str) -> Plugin | None:
        return self._plugins.get(plugin_id)

    def manifests(self, capability: str | None = None) -> tuple[PluginManifest, ...]:
        return tuple(
            plugin.manifest
            for plugin in self._plugins.values()
            if capability is None or capability in plugin.manifest.capabilities
        )

    def start_all(self, context: PluginContext) -> tuple[str, ...]:
        """Start every plugin, skipping and logging failures."""
        started: list[str] = []
        for plugin_id, plugin in list(self._plugins.items()):
            try:
                plugin.start(context)
                started.append(plugin_id)
            except Exception:
                logger.exception("plugin failed to start: %s", plugin_id)
        return tuple(started)

    def stop_all(self) -> None:
        for plugin_id, plugin in list(self._plugins.items()):
            try:
                plugin.stop()
            except Exception:
                logger.exception("plugin failed to stop: %s", plugin_id)
