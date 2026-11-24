"""Windows toast delivery with a no-op fallback."""

from __future__ import annotations

import logging
import threading
import time

from ..contracts.outputs import Notifier
from .config import Notify

logger = logging.getLogger(__name__)

DEFAULT_TOAST_TIMEOUT = 5.0
DEFAULT_TOAST_COOLDOWN = 600.0


class NullNotifier:
    def notify(self, title: str, body: str) -> bool:
        return False


class WindowsToastNotifier:
    """Lazy wrapper around `win11toast`; import cost is paid on first toast.

    `win11toast.toast` can block waiting for the Windows notification service.
    A hung toast must not stop the ingestion/triage loop, so delivery runs in a
    daemon thread with a timeout. A timed-out suggestion stays queued.
    """

    def __init__(
        self,
        timeout: float = DEFAULT_TOAST_TIMEOUT,
        cooldown: float = DEFAULT_TOAST_COOLDOWN,
    ) -> None:
        self.timeout = timeout
        self.cooldown = cooldown
        self._blocked_until = 0.0

    def notify(self, title: str, body: str) -> bool:
        if time.monotonic() < self._blocked_until:
            return False
        try:
            from win11toast import toast
        except ImportError:
            logger.warning("win11toast is not installed; toast delivery unavailable")
            return False

        result = {"ok": False, "error": None}

        def send() -> None:
            try:
                # win11toast waits until the toast is clicked, dismissed, or
                # fails. That is normal and can outlive this short handoff.
                toast(
                    title,
                    body,
                    on_click=lambda *_: None,
                    on_dismissed=lambda *_: None,
                    on_failed=lambda *_: None,
                )
                result["ok"] = True
            except Exception as exc:
                result["error"] = exc
                logger.warning("toast delivery failed: %s", type(exc).__name__)

        thread = threading.Thread(target=send, name="alice-toast", daemon=True)
        thread.start()
        # Give WinRT a moment to raise an immediate error. A live thread is
        # the normal state after the toast is displayed, not a failure.
        thread.join(min(1.0, max(0.25, self.timeout)))
        if thread.is_alive():
            return True
        if result["error"] is not None or not result["ok"]:
            self._blocked_until = time.monotonic() + self.cooldown
            return False
        return True


def build_notifier(config: Notify, *, dry_run: bool = False) -> Notifier:
    if dry_run or "toast" not in config.mode:
        return NullNotifier()
    return WindowsToastNotifier()
