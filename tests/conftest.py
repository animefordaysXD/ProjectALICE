"""Shared test fixtures. Everything runs offline on temporary directories."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

MINIMAL_CONFIG = """\
models:
  triage:      {provider: fake, model: fake-triage}
  reason:      {provider: fake, model: fake-reason}
  consolidate: {provider: fake, model: fake-consolidate, batch: true}

budget:
  soft_daily_usd: 0.05
  hard_daily_usd: 0.10

sources:
  - {type: logfile, path: './logs', glob: '*.log'}
"""


@pytest.fixture(autouse=True)
def _isolate_alice_env(monkeypatch: pytest.MonkeyPatch):
    """Keep the developer's Alice environment out of the tests."""
    for name in ("ALICE_DATA_DIR", "ALICE_PROFILE"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def _close_alice_log_handlers():
    """Release log files before pytest deletes the tmp directory (Windows)."""
    yield
    logger = logging.getLogger("alice")
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()


@pytest.fixture
def alice_root(tmp_path: Path) -> Path:
    root = tmp_path / "alice"
    (root / "config").mkdir(parents=True)
    (root / "config" / "alice.yaml").write_text(MINIMAL_CONFIG, encoding="utf-8")
    return root


@pytest.fixture
def config_path(alice_root: Path) -> Path:
    return alice_root / "config" / "alice.yaml"

