"""Read-only tool contracts used by chat and exploration."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, Sequence


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    read_only: bool = True


@dataclass(frozen=True)
class ToolResult:
    ok: bool
    text: str
    data: Mapping[str, Any] = field(default_factory=dict)


class Tool(Protocol):
    spec: ToolSpec

    def run(self, args: Mapping[str, Any]) -> ToolResult: ...


class ToolExecutor(Protocol):
    def specs(self) -> Sequence[ToolSpec]: ...

    def execute(self, name: str, args: Mapping[str, Any]) -> ToolResult: ...
