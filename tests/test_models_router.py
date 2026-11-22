from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

import pytest

from alice.app.settings import load_settings
from alice.models.config import Budget, ModelSpec, Pricing
from alice.core.db import connect, init_db
from alice.inputs.redact import Redactor, redact
from alice.models import Router, RouterError, build_router, completion_cost
from alice.models.base import Completion, Message, Provider, ProviderError
from alice.models.budget import BudgetError, BudgetGuard, UsageLog, clear_pause, set_pause
from alice.models.fake import FakeProvider

NOSLEEP = lambda _seconds: None  # noqa: E731


@pytest.fixture
def conn(tmp_path: Path):
    db_path = tmp_path / "alice.db"
    init_db(db_path)
    connection = connect(db_path, create=False)
    yield connection
    connection.close()


def make_guard(
    conn: sqlite3.Connection, tmp_path: Path, *, soft: float = 0.05, hard: float = 10.0
) -> BudgetGuard:
    return BudgetGuard(
        conn,
        Budget(soft_daily_usd=soft, hard_daily_usd=hard),
        tmp_path / "state",
    )


def make_router(
    conn: sqlite3.Connection,
    tmp_path: Path,
    *,
    provider: Provider | None = None,
    pricing: Pricing | None = None,
    soft: float = 0.05,
    hard: float = 10.0,
    sleep=NOSLEEP,
) -> tuple[Router, Provider]:
    fake = provider or FakeProvider()
    specs = {
        "triage": ModelSpec(
            provider="fake",
            model="fake-triage",
            pricing=pricing if pricing is not None else Pricing(1.0, 2.0),
        )
    }
    router = Router(
        models=specs,
        providers={"fake": fake, "anthropic": fake},
        usage=UsageLog(conn),
        budget=make_guard(conn, tmp_path, soft=soft, hard=hard),
        sleep=sleep,
    )
    return router, fake


def test_routes_tier_and_logs_usage(conn: sqlite3.Connection, tmp_path: Path) -> None:
    fake = FakeProvider(responses=["hello"])
    router, _ = make_router(conn, tmp_path, provider=fake)

    completion = router.complete(
        "triage", [Message("user", "hi")], 10, purpose="test"
    )

    assert completion.text == "hello"
    assert fake.calls[0].model == "fake-triage"
    assert completion.cost_usd > 0

    row = conn.execute("SELECT * FROM usage").fetchone()
    assert row["tier"] == "triage"
    assert row["model"] == "fake-triage"
    assert row["purpose"] == "test"
    assert row["cost_usd"] == pytest.approx(completion.cost_usd)


def test_cost_math_includes_cached_tokens() -> None:
    completion = Completion(
        text="x", input_tokens=1000, cached_tokens=400, output_tokens=500
    )
    pricing = Pricing(
        input_per_mtok=1.0, output_per_mtok=2.0, cached_input_per_mtok=0.5
    )

    assert completion_cost(completion, pricing) == pytest.approx(0.0018)


def test_cached_tokens_without_price_use_input_price() -> None:
    completion = Completion(text="x", input_tokens=1000, cached_tokens=1000, output_tokens=0)
    pricing = Pricing(input_per_mtok=3.0, output_per_mtok=15.0)

    assert completion_cost(completion, pricing) == pytest.approx(0.003)


class _MeteredProvider(Provider):
    name = "metered"

    def __init__(self) -> None:
        self.calls = 0

    def complete(self, messages, model, max_tokens, cache_prefix_len=None):
        self.calls += 1
        raise AssertionError("unpriced provider must not be called")


def test_unpriced_paid_tier_is_refused(conn: sqlite3.Connection, tmp_path: Path) -> None:
    metered = _MeteredProvider()
    router = Router(
        models={"reason": ModelSpec(provider="metered", model="paid-model")},
        providers={"metered": metered},
        usage=UsageLog(conn),
        budget=make_guard(conn, tmp_path),
        sleep=NOSLEEP,
    )

    with pytest.raises(RouterError, match="no pricing"):
        router.complete("reason", [Message("user", "hi")], 10)

    assert metered.calls == 0


def test_unknown_tier(conn: sqlite3.Connection, tmp_path: Path) -> None:
    router, _ = make_router(conn, tmp_path)

    with pytest.raises(RouterError, match="unknown model tier"):
        router.complete("nope", [Message("user", "hi")], 10)


def test_missing_provider(conn: sqlite3.Connection, tmp_path: Path) -> None:
    router = Router(
        models={"triage": ModelSpec(provider="fake", model="m", pricing=Pricing(1, 1))},
        providers={},
        usage=UsageLog(conn),
        budget=make_guard(conn, tmp_path),
        sleep=NOSLEEP,
    )

    with pytest.raises(RouterError, match="not available"):
        router.complete("triage", [Message("user", "hi")], 10)


def test_hard_cap_blocks_before_the_provider_is_called(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    UsageLog(conn).record(
        tier="triage",
        model="m",
        input_tokens=0,
        cached_tokens=0,
        output_tokens=0,
        cost_usd=0.10,
    )
    router, fake = make_router(conn, tmp_path, hard=0.10)

    with pytest.raises(BudgetError, match="hard daily cap"):
        router.complete("triage", [Message("user", "hi")], 10)

    assert fake.calls == []


def test_estimated_cost_can_block_a_call(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    UsageLog(conn).record(
        tier="triage",
        model="m",
        input_tokens=0,
        cached_tokens=0,
        output_tokens=0,
        cost_usd=0.0995,
    )
    router, fake = make_router(
        conn,
        tmp_path,
        pricing=Pricing(input_per_mtok=1000.0, output_per_mtok=1000.0),
        hard=0.10,
    )

    with pytest.raises(BudgetError, match="would exceed"):
        router.complete("triage", [Message("user", "x" * 100)], 10)

    assert fake.calls == []


def test_pause_blocks_calls_until_resume(conn: sqlite3.Connection, tmp_path: Path) -> None:
    router, fake = make_router(conn, tmp_path)
    set_pause(tmp_path / "state", "testing")

    with pytest.raises(BudgetError, match="paused"):
        router.complete("triage", [Message("user", "hi")], 10)
    assert fake.calls == []

    clear_pause(tmp_path / "state")
    router.complete("triage", [Message("user", "hi")], 10)
    assert len(fake.calls) == 1


def test_soft_cap_warns_once_per_day(
    conn: sqlite3.Connection, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    UsageLog(conn).record(
        tier="triage",
        model="m",
        input_tokens=0,
        cached_tokens=0,
        output_tokens=0,
        cost_usd=0.05,
    )
    router, _ = make_router(conn, tmp_path, soft=0.05, hard=10.0)

    with caplog.at_level(logging.WARNING, logger="alice.models.budget"):
        router.complete("triage", [Message("user", "hi")], 10)
        router.complete("triage", [Message("user", "hi")], 10)

    warnings = [record.message for record in caplog.records if "soft cap" in record.message]
    assert len(warnings) == 1


def test_transient_error_is_retried_once(conn: sqlite3.Connection, tmp_path: Path) -> None:
    fake = FakeProvider(fail_times=1)
    sleeps: list[float] = []
    router, _ = make_router(conn, tmp_path, provider=fake, sleep=sleeps.append)

    completion = router.complete("triage", [Message("user", "hi")], 10)

    assert completion.text
    assert len(fake.calls) == 2
    assert sleeps == [1.0]


def test_two_transient_errors_stop_after_one_retry(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    fake = FakeProvider(fail_times=2)
    router, _ = make_router(conn, tmp_path, provider=fake)

    with pytest.raises(ProviderError, match="simulated"):
        router.complete("triage", [Message("user", "hi")], 10)

    assert len(fake.calls) == 2


class _PermanentFailure(Provider):
    name = "fake"

    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    def complete(self, messages, model, max_tokens, cache_prefix_len=None):
        self.calls.append((model, max_tokens))
        raise ProviderError("bad request", transient=False)


def test_non_transient_error_is_not_retried(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    fake = _PermanentFailure()
    router, _ = make_router(conn, tmp_path, provider=fake)

    with pytest.raises(ProviderError, match="bad request"):
        router.complete("triage", [Message("user", "hi")], 10)

    assert len(fake.calls) == 1


def test_outgoing_payload_has_no_pii(conn: sqlite3.Connection, tmp_path: Path) -> None:
    raw = "mail jane.doe@example.com card 4111-1111-1111-1111 phone 010-1234-5678"
    redacted = Redactor().redact(raw)
    assert redacted.changed

    router, fake = make_router(conn, tmp_path)
    router.complete("triage", [Message("user", redacted.text)], 50)

    payload = "\n".join(
        message.content for call in fake.calls for message in call.messages
    )
    assert redact(payload).changed is False
    # Guard against a vacuous scan.
    assert redact(raw).changed is True


def test_budget_status_reflects_calls(conn: sqlite3.Connection, tmp_path: Path) -> None:
    router, _ = make_router(conn, tmp_path)
    completion = router.complete("triage", [Message("user", "hi")], 10)
    status = make_guard(conn, tmp_path).status()

    assert status.calls_today == 1
    assert status.spend_today == pytest.approx(completion.cost_usd)
    assert status.paused is None


def test_build_router_from_config(conn: sqlite3.Connection, config_path: Path) -> None:
    config = load_settings(config_path)
    router = build_router(config.models, data_dir=config.data_dir, conn=conn)

    completion = router.complete("triage", [Message("user", "hi")], 10)

    assert completion.text == "fake ok"
    assert conn.execute("SELECT COUNT(*) FROM usage").fetchone()[0] == 1

