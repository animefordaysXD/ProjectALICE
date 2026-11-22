from __future__ import annotations

import logging

import pytest

from alice.contracts.plugins import PluginManifest
from alice.plugins import PluginError, PluginRegistry


class _Context:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def log(self, message: str) -> None:
        self.lines.append(message)


class _Plugin:
    def __init__(self, plugin_id: str, *, capability: str = "sensor", fail: bool = False) -> None:
        self.manifest = PluginManifest(
            id=plugin_id, version="1.0.0", capabilities=frozenset({capability})
        )
        self.fail = fail
        self.started = False
        self.stopped = False

    def start(self, context) -> None:
        if self.fail:
            raise RuntimeError("boom")
        self.started = True

    def stop(self) -> None:
        self.stopped = True


def test_register_and_filter_by_capability() -> None:
    registry = PluginRegistry()
    sensor = _Plugin("log-sensor")
    notifier = _Plugin("toast", capability="notifier")
    registry.register(sensor)
    registry.register(notifier)

    assert [m.id for m in registry.manifests()] == ["log-sensor", "toast"]
    assert [m.id for m in registry.manifests("notifier")] == ["toast"]
    assert registry.get("log-sensor") is sensor


def test_duplicate_id_is_rejected() -> None:
    registry = PluginRegistry()
    registry.register(_Plugin("same"))

    with pytest.raises(PluginError, match="duplicate"):
        registry.register(_Plugin("same"))


def test_unsupported_capability_is_rejected() -> None:
    with pytest.raises(ValueError, match="unsupported"):
        PluginManifest(id="bad", version="1", capabilities=frozenset({"action"}))


def test_start_failure_is_contained(caplog: pytest.LogCaptureFixture) -> None:
    registry = PluginRegistry()
    registry.register(_Plugin("good"))
    registry.register(_Plugin("bad", fail=True))
    context = _Context()

    with caplog.at_level(logging.ERROR, logger="alice.plugins.registry"):
        started = registry.start_all(context)

    assert started == ("good",)
    assert any("bad" in record.message or "boom" in record.getMessage() for record in caplog.records)


def test_stop_all_is_best_effort() -> None:
    registry = PluginRegistry()
    first = _Plugin("one")
    second = _Plugin("two")
    registry.register(first)
    registry.register(second)

    registry.stop_all()

    assert first.stopped and second.stopped
