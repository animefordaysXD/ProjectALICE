"""Extract a JSON object from a model reply."""

from __future__ import annotations

import json


class OutputFormatError(ValueError):
    """The reply did not contain a valid JSON object."""


def extract_object(text: str) -> dict:
    raw = text.strip()
    if raw.startswith("```"):
        parts = raw.split("\n", 1)
        raw = parts[1] if len(parts) == 2 else ""
        raw = raw.rstrip().rstrip("`").strip()
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end <= start:
        raise OutputFormatError("reply did not contain a JSON object")
    try:
        payload = json.loads(raw[start : end + 1])
    except json.JSONDecodeError as exc:
        raise OutputFormatError(f"invalid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise OutputFormatError("reply must be a JSON object")
    return payload
