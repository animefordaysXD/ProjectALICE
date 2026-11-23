from __future__ import annotations

import sys
import time
import types

from alice.outputs.notify import NullNotifier, WindowsToastNotifier


def test_null_notifier_never_delivers() -> None:
    assert NullNotifier().notify("Alice", "test") is False


def test_toast_success(monkeypatch) -> None:
    fake = types.ModuleType("win11toast")
    fake.toast = lambda *_args, **_kwargs: None
    monkeypatch.setitem(sys.modules, "win11toast", fake)

    assert WindowsToastNotifier(timeout=0.5).notify("Alice", "test") is True


def test_toast_timeout_does_not_block(monkeypatch) -> None:
    fake = types.ModuleType("win11toast")
    fake.toast = lambda *_args, **_kwargs: time.sleep(1.0)
    monkeypatch.setitem(sys.modules, "win11toast", fake)

    notifier = WindowsToastNotifier(timeout=0.05, cooldown=60)
    started = time.monotonic()
    delivered = notifier.notify("Alice", "test")
    elapsed = time.monotonic() - started

    assert delivered is False
    assert elapsed < 0.5

    # The failed notifier is put on cooldown, so a second attempt returns
    # immediately instead of paying another 5-second timeout per suggestion.
    started = time.monotonic()
    assert notifier.notify("Alice", "another") is False
    assert time.monotonic() - started < 0.02
