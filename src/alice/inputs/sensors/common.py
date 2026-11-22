"""Small shared helpers for poll-driven sensors."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Mapping

HEAD_BYTES = 256
DEFAULT_MAX_LINES = 500
MAX_BYTES_PER_POLL = 1_000_000
MAX_LINE_CHARS = 8000


def resolve_path(value: str, root: Path) -> Path:
    candidate = Path(value).expanduser()
    return candidate if candidate.is_absolute() else (root / candidate)


def file_head(path: Path, length: int = HEAD_BYTES) -> str:
    """Hash the first `length` bytes. Used to detect replacement in place."""
    try:
        with path.open("rb") as handle:
            return hashlib.sha256(handle.read(max(0, length))).hexdigest()
    except OSError:
        return ""


def encode_cursor(state: Mapping[str, object]) -> str:
    return json.dumps(dict(state), sort_keys=True)
