from __future__ import annotations

from pathlib import Path

import pytest

from alice.inputs.config import FilesConfig
from alice.inputs.files import FileAccessError, FileReader


def reader(root: Path, *, max_bytes: int = 1024, max_files: int = 2, sanitize=None):
    return FileReader(
        FilesConfig(roots=(root,), max_bytes=max_bytes, max_files=max_files),
        sanitize=sanitize,
    )


def test_read_allowed_text_and_sanitize(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    path = root / "notes.txt"
    path.write_text("hello secret", encoding="utf-8")

    result = reader(root, sanitize=lambda text: text.replace("secret", "[SECRET]")).read(path)

    assert result.text == "hello [SECRET]"
    assert result.truncated is False


def test_reject_outside_root(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("nope", encoding="utf-8")

    with pytest.raises(FileAccessError, match="outside files.roots"):
        reader(root).read(outside)


def test_reject_binary_and_large_truncation(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    binary = root / "binary.txt"
    binary.write_bytes(b"\x00\x01\x02")
    large = root / "large.txt"
    large.write_text("x" * 100, encoding="utf-8")

    with pytest.raises(FileAccessError, match="binary"):
        reader(root).read(binary)

    result = reader(root, max_bytes=10).read(large)
    assert result.truncated is True
    assert result.text == "x" * 10


def test_file_limit(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    first = root / "a.txt"
    second = root / "b.txt"
    first.write_text("a", encoding="utf-8")
    second.write_text("b", encoding="utf-8")

    with pytest.raises(FileAccessError, match="at most 2"):
        reader(root).read_many([first, second, first])
