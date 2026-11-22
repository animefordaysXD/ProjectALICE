from __future__ import annotations

import logging
from pathlib import Path

from alice.core.logging import BACKUP_COUNT, MAX_BYTES, setup_logging


def test_writes_to_file(tmp_path: Path) -> None:
    log_path = setup_logging(tmp_path / "logs")
    logging.getLogger("alice.test").info("pipeline started")

    assert log_path.is_file()
    assert "pipeline started" in log_path.read_text(encoding="utf-8")


def test_uses_capped_rotating_files(tmp_path: Path) -> None:
    setup_logging(tmp_path / "logs")
    handlers = logging.getLogger("alice").handlers
    file_handler = next(h for h in handlers if hasattr(h, "maxBytes"))

    assert file_handler.maxBytes == MAX_BYTES
    assert file_handler.backupCount == BACKUP_COUNT


def test_repeated_setup_does_not_duplicate_handlers(tmp_path: Path) -> None:
    setup_logging(tmp_path / "logs")
    setup_logging(tmp_path / "logs")

    assert len(logging.getLogger("alice").handlers) == 2


def test_korean_text_is_written_as_utf8(tmp_path: Path) -> None:
    log_path = setup_logging(tmp_path / "logs")
    logging.getLogger("alice.test").info("한국어 로그 ok")

    assert "한국어 로그 ok" in log_path.read_text(encoding="utf-8")

