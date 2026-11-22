"""Windows toast delivery with a no-op fallback."""

from __future__ import annotations

import logging

from ..contracts.outputs import Notifier
from .config import Notify

logger = logging.getLogger(__name__)


class NullNotifier:
    def notify(self, title: str, body: str) -> bool:
        return False


class WindowsToastNotifier:
    """Lazy wrapper around `win11toast`; import cost is paid on first toast."""

    def notify(self, title: str, body: str) -> bool:
        try:
            from win11toast import toast
        except ImportError:
            logger.warning("win11toast is not installed; toast delivery unavailable")
            return False
        try:
            toast(title, body)
            return True
        except Exception as exc:
            logger.warning("toast delivery failed: %s", type(exc).__name__)
            return False


def build_notifier(config: Notify, *, dry_run: bool = False) -> Notifier:
    if dry_run or "toast" not in config.mode:
        return NullNotifier()
    return WindowsToastNotifier()
