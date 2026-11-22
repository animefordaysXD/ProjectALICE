"""Delivery: suggestion queue, digest, and notifications."""

from .config import Notify, OutputsConfig, parse_outputs
from .digest import DigestResult, DigestWriter
from .notify import NullNotifier, WindowsToastNotifier, build_notifier
from .queue import QueuedSuggestion, SuggestionQueue, is_quiet

__all__ = [
    "DigestResult",
    "DigestWriter",
    "Notify",
    "NullNotifier",
    "OutputsConfig",
    "QueuedSuggestion",
    "SuggestionQueue",
    "WindowsToastNotifier",
    "build_notifier",
    "is_quiet",
    "parse_outputs",
]
