"""Untrusted-content wrapper for anything observed outside Alice.

Markers are a boundary, not a sandbox. The model is told that text inside them
is data and that instructions inside it must be reported, never followed. The
closing marker is defanged so content cannot end the block early.
"""

from __future__ import annotations

from typing import Any

OPEN = '<observed_data source="{source}"'
CLOSE = "</observed_data>"
_ESC_CLOSE = "</observed_data_escaped>"
_ESC_OPEN = "<observed_data_escaped"


def wrap(content: Any, source: str, **attributes: Any) -> str:
    text = str(content or "")
    text = text.replace(CLOSE, _ESC_CLOSE).replace("<observed_data", _ESC_OPEN)
    attrs = "".join(
        f' {key}="{_clean(str(value))}"' for key, value in attributes.items()
    )
    return f'{OPEN.format(source=_clean(source) or "external")}{attrs}>\n{text}\n{CLOSE}'


def unwrap(text: str) -> str:
    """Remove the wrapper for display to a person."""
    text = str(text or "")
    start = text.find("<observed_data")
    end = text.find(CLOSE)
    if start < 0 or end < 0 or end < start:
        return text
    first_newline = text.find("\n", start)
    if first_newline < 0 or first_newline > end:
        return text
    inner = text[first_newline + 1 : end]
    return inner.replace(_ESC_CLOSE, CLOSE).replace(_ESC_OPEN, "<observed_data")


def _clean(value: str) -> str:
    return value.replace('"', "'").replace("\n", " ").replace("\r", " ").strip()
