"""Enforce the module dependency graph in ARCHITECTURE.md.

If this fails, do not silence it: either the import is wrong, or the shared
piece belongs in `core`/`contracts` and should move there.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "alice"

ALLOWED: dict[str, set[str]] = {
    "contracts": set(),
    "core": {"contracts"},
    "inputs": {"contracts", "core"},
    "memory": {"contracts", "core"},
    "models": {"contracts", "core"},
    "brain": {"contracts", "core"},
    "outputs": {"contracts", "core"},
    "plugins": {"contracts", "core"},
    "app": {
        "contracts",
        "core",
        "inputs",
        "memory",
        "models",
        "brain",
        "outputs",
        "plugins",
    },
}


def _owner(path: Path) -> str | None:
    relative = path.relative_to(SRC)
    if relative.name in ("__init__.py", "__main__.py") and len(relative.parts) == 1:
        return "app" if relative.name == "__main__.py" else None
    if len(relative.parts) < 2:
        return None
    return relative.parts[0]


def _package(path: Path) -> list[str]:
    return list(path.relative_to(SRC).parent.parts)


def _imports(path: Path) -> list[tuple[int, list[str]]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[tuple[int, list[str]]] = []
    package = _package(path)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                parts = alias.name.split(".")
                if parts[0] == "alice" and len(parts) > 1:
                    found.append((node.lineno, parts[1:]))
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                # level 1 = current package, 2 = parent, ...
                base = package[: len(package) - (node.level - 1)] if node.level > 1 else package
                module = node.module.split(".") if node.module else []
                parts = base + module
            else:
                parts = node.module.split(".") if node.module else []
            if parts and parts[0] == "alice" and len(parts) > 1:
                found.append((node.lineno, parts[1:]))
    return found


def test_module_dependency_graph() -> None:
    violations: list[str] = []

    for path in sorted(SRC.rglob("*.py")):
        owner = _owner(path)
        if owner is None:
            continue
        if owner not in ALLOWED:
            violations.append(f"{path}: unknown module directory '{owner}'")
            continue
        for line, parts in _imports(path):
            imported = parts[0]
            if imported == owner:
                continue
            if imported not in ALLOWED:
                violations.append(
                    f"{path}:{line}: import of unknown Alice module '{imported}'"
                )
                continue
            if imported not in ALLOWED[owner]:
                violations.append(
                    f"{path}:{line}: {owner} may not import {imported}"
                )

    assert not violations, "\n".join(violations)


def test_contracts_are_self_contained() -> None:
    for path in (SRC / "contracts").glob("*.py"):
        for line, parts in _imports(path):
            if parts[0] == "contracts":
                continue
            raise AssertionError(f"{path}:{line}: contracts imported alice.{'.'.join(parts)}")
