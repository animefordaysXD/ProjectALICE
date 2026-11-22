"""The user-editable JSON file that is Alice's always-on core memory.

Alice creates a template once and reads it; she never writes over an existing
file. Jo can edit it at any time. The file is included in every prompt, so it
is capped; going over the cap fails loudly instead of silently dropping facts.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .errors import CoreMemoryError, MemoryCapError
from ..core.tokens import estimate_tokens

CORE_TEMPLATE: dict[str, Any] = {
    "version": 1,
    "identity": {
        "name": "Alice",
        "owner": "Jo",
        "role": "local secretary",
    },
    "rules": [
        "Suggest, never act.",
        "Never send raw logs or messages to a model.",
        "Never spend money silently.",
    ],
    "facts": {
        "timezone": "Asia/Seoul",
        "languages": "Korean and English",
    },
    "people": {},
    "notes": [
        "Edit this file to change Alice's always-on core memory.",
        "Keep it under 2,000 tokens; Alice fails loudly rather than truncate.",
    ],
}


class CoreMemory:
    def __init__(self, path: Path | str, max_tokens: int = 2000) -> None:
        self.path = Path(path)
        self.max_tokens = max_tokens

    def exists(self) -> bool:
        return self.path.is_file()

    def ensure(self) -> bool:
        """Create a template if missing. Never overwrite an edited file."""
        if self.path.is_file():
            return False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_name(f"{self.path.name}.{os.getpid()}.tmp")
        temp.write_text(
            json.dumps(CORE_TEMPLATE, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        os.replace(temp, self.path)
        return True

    def load(self) -> dict[str, Any]:
        if not self.path.is_file():
            raise CoreMemoryError(
                f"core memory file not found: {self.path}; run `alice init` "
                "or create it by hand"
            )
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise CoreMemoryError(f"invalid JSON in {self.path}: {exc}") from exc
        except OSError as exc:
            raise CoreMemoryError(f"cannot read {self.path}: {exc}") from exc
        if not isinstance(data, dict):
            raise CoreMemoryError(f"{self.path} must contain a JSON object")
        self._validate(data)
        return data

    def render(self) -> str:
        data = self.load()
        lines = ["[CORE MEMORY]"]
        identity = data.get("identity") or {}
        if identity:
            lines.append("Identity:")
            for key in sorted(identity):
                lines.append(f"  {_pretty(key)}: {identity[key]}")
        rules = data.get("rules") or []
        if rules:
            lines.append("Rules:")
            lines.extend(f"  - {rule}" for rule in rules)
        facts = data.get("facts") or {}
        if facts:
            lines.append("Facts:")
            for key in sorted(facts):
                lines.append(f"  {_pretty(key)}: {facts[key]}")
        people = data.get("people") or {}
        if people:
            lines.append("People:")
            for key in sorted(people):
                value = people[key]
                if isinstance(value, dict):
                    details = "; ".join(f"{k}={v}" for k, v in value.items())
                    lines.append(f"  - {key}: {details}")
                else:
                    lines.append(f"  - {key}: {value}")
        notes = data.get("notes") or []
        if notes:
            lines.append("Notes:")
            lines.extend(f"  - {note}" for note in notes)
        text = "\n".join(lines)
        tokens = estimate_tokens(text)
        if tokens > self.max_tokens:
            raise MemoryCapError(
                f"core memory is ~{tokens} tokens, over the {self.max_tokens}-token "
                f"cap; prune {self.path} (Alice will not truncate it)"
            )
        return text

    def _validate(self, data: dict[str, Any]) -> None:
        if "version" in data and not isinstance(data["version"], int):
            raise CoreMemoryError(f"{self.path}: version must be an integer")
        for key in ("identity", "facts", "people"):
            if key in data and not isinstance(data[key], dict):
                raise CoreMemoryError(f"{self.path}: {key} must be an object")
        for key in ("rules", "notes"):
            if key in data:
                value = data[key]
                if not isinstance(value, list) or not all(
                    isinstance(item, str) for item in value
                ):
                    raise CoreMemoryError(f"{self.path}: {key} must be a list of strings")


def _pretty(key: str) -> str:
    return key.replace("_", " ").replace("-", " ").strip().capitalize()
