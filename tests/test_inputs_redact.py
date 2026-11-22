from __future__ import annotations

import re
from pathlib import Path

import pytest

from alice.inputs.config import SecretPattern
from alice.inputs.redact import Redactor, redact

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.mark.parametrize(
    ("raw", "placeholder", "name"),
    [
        ("주민번호 900101-1234567 확인", "[RRN]", "RRN"),
        ("9001011234567", "[RRN]", "RRN"),
        ("연락처 010-1234-5678", "[PHONE]", "PHONE"),
        ("01012345678", "[PHONE]", "PHONE"),
        ("사무실 02-555-1234", "[PHONE]", "PHONE"),
        ("+82 10-1234-5678", "[PHONE]", "PHONE"),
        ("card 4111-1111-1111-1111", "[CARD]", "CARD"),
        ("amex 3782 822463 10005", "[AMEX]", "AMEX"),
        ("계좌 110-123-456789", "[ACCOUNT]", "ACCOUNT"),
        ("mail jane.doe@example.com now", "[EMAIL]", "EMAIL"),
    ],
)
def test_builtin_masks(raw: str, placeholder: str, name: str) -> None:
    result = redact(raw)

    assert placeholder in result.text
    assert result.counts[name] >= 1


def test_multiple_hits_are_counted() -> None:
    result = redact("010-1111-2222 and 010-3333-4444, mail a@b.co")

    assert result.text.count("[PHONE]") == 2
    assert result.counts["PHONE"] == 2
    assert result.counts["EMAIL"] == 1
    assert result.total == 3


def test_timestamps_and_versions_survive() -> None:
    for text in (
        "2026-11-21T09:30:00Z INFO started",
        "2026-11-21 09:30:00 [INFO] 시작",
        "deployed version 1.2.3 at 09:30",
        "order 2026-11-21 4 items",
        "pid=4821 port=8080 ratio=0.75",
        "client 192.168.0.14 connected",
    ):
        assert redact(text).text == text


def test_custom_pattern_becomes_named_placeholder() -> None:
    redactor = Redactor(
        [SecretPattern(name="AWS_KEY", pattern=re.compile(r"\bAKIA[0-9A-Z]{16}\b"))]
    )

    result = redactor.redact("key AKIAABCDEFGHIJKLMNOP leaked")

    assert result.text == "key [AWS_KEY] leaked"
    assert result.counts == {"AWS_KEY": 1}


def test_redaction_is_idempotent() -> None:
    once = redact("900101-1234567 / 010-1234-5678 / a@b.co").text

    assert redact(once).text == once
    assert redact(once).counts == {}


def test_korean_fixture_is_masked() -> None:
    text = (FIXTURES / "logs" / "app_ko.log").read_text(encoding="utf-8")
    masked = redact(text).text

    assert "[RRN]" in masked
    assert "[PHONE]" in masked
    assert "900101-1234567" not in masked
    assert "010-9876-5432" not in masked


@pytest.mark.parametrize(
    "needle",
    [
        "900101-1234567",
        "010-9876-5432",
        "010-2345-6789",
        "010-1111-2222",
        "jane.doe@example.com",
        "john.smith@example.org",
        "4111-1111-1111-1111",
        "110-123-456789",
        "02-555-1234",
    ],
)
def test_no_fixture_pii_survives_redaction(needle: str) -> None:
    for path in FIXTURES.rglob("*"):
        if path.is_file():
            text = path.read_text(encoding="utf-8")
            assert needle not in redact(text).text


def test_english_fixture_placeholders() -> None:
    text = (FIXTURES / "logs" / "app_en.log").read_text(encoding="utf-8")
    masked = redact(text).text

    assert "[EMAIL]" in masked
    assert "[CARD]" in masked
    assert "[PHONE]" in masked

