"""Logging setup.

Alice keeps a size-capped rotating log under the data directory. Logs hold
metadata only: no message or log content may be written here.
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOGGER_NAME = "alice"
LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s | %(message)s"
MAX_BYTES = 1_000_000
BACKUP_COUNT = 3


def setup_logging(log_dir: Path, *, verbose: bool = False) -> Path:
    """Configure file and console logging; safe to call more than once."""
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "alice.log"

    logger = logging.getLogger(LOGGER_NAME)
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    formatter = logging.Formatter(LOG_FORMAT)

    file_handler = RotatingFileHandler(
        log_path, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8"
    )
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(formatter)

    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.DEBUG if verbose else logging.WARNING)
    console_handler.setFormatter(formatter)

    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    logger.propagate = True
    return log_path
