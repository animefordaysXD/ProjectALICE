from __future__ import annotations

import json
from pathlib import Path

import pytest

from alice.memory.core_file import CoreMemory
from alice.memory.errors import CoreMemoryError, MemoryCapError


def test_ensure_creates_a_template_without_overwriting(tmp_path: Path) -> None:
    path = tmp_path / "memory" / "core.json"
    core = CoreMemory(path)

    assert core.ensure() is True
    assert path.is_file()
    assert json.loads(path.read_text(encoding="utf-8"))["identity"]["name"] == "Alice"

    path.write_text('{"version": 1, "identity": {"name": "Edited"}}', encoding="utf-8")
    assert core.ensure() is False
    assert "Edited" in path.read_text(encoding="utf-8")


def test_render_includes_sections_and_korean(tmp_path: Path) -> None:
    path = tmp_path / "core.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "identity": {"name": "앨리스", "owner": "조"},
                "rules": ["행동하지 말 것"],
                "facts": {"project": "Alice"},
                "people": {"동료": {"role": "developer"}},
                "notes": ["한국어와 영어를 쓴다"],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    text = CoreMemory(path).render()

    assert "[CORE MEMORY]" in text
    assert "앨리스" in text
    assert "행동하지 말 것" in text
    assert "Project: Alice" in text
    assert "동료" in text
    assert "한국어와 영어를 쓴다" in text
    assert str(path) not in text


def test_missing_file_is_loud(tmp_path: Path) -> None:
    with pytest.raises(CoreMemoryError, match="not found"):
        CoreMemory(tmp_path / "missing.json").render()


def test_invalid_json_is_loud(tmp_path: Path) -> None:
    path = tmp_path / "core.json"
    path.write_text("{not json", encoding="utf-8")

    with pytest.raises(CoreMemoryError, match="invalid JSON"):
        CoreMemory(path).load()


def test_wrong_shape_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "core.json"
    path.write_text('{"rules": "not a list"}', encoding="utf-8")

    with pytest.raises(CoreMemoryError, match="rules must be a list"):
        CoreMemory(path).load()


def test_over_cap_fails_instead_of_truncating(tmp_path: Path) -> None:
    path = tmp_path / "core.json"
    path.write_text(
        json.dumps({"rules": ["x" * 400]}, ensure_ascii=False), encoding="utf-8"
    )

    with pytest.raises(MemoryCapError, match="over the 10-token cap"):
        CoreMemory(path, max_tokens=10).render()
