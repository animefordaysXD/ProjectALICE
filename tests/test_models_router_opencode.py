from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from alice.models.config import Budget, ModelSpec
from alice.core.db import connect, init_db
from alice.models import Router
from alice.models.base import Message, ProviderError, ProviderNotConfigured
from alice.models.budget import BudgetGuard, UsageLog
from alice.models.opencode import (
    DEFAULT_TIMEOUT,
    OpenCodeProvider,
    find_cli,
    flatten_messages,
    parse_events,
)


class _Result:
    def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


class RecordingRunner:
    def __init__(self, result=None) -> None:
        self.result = result if result is not None else _Result()
        self.calls: list[tuple[list[str], str, float, str | None]] = []

    def __call__(self, command, prompt, timeout, cwd):
        self.calls.append((command, prompt, timeout, cwd))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def event_stream(
    texts: list[str],
    *,
    input_tokens: int = 100,
    output_tokens: int = 10,
    cached: int = 40,
    cost: float = 0.0,
) -> str:
    lines = [
        json.dumps({"type": "text", "part": {"type": "text", "text": text}})
        for text in texts
    ]
    lines.append(
        json.dumps(
            {
                "type": "step_finish",
                "part": {
                    "type": "step-finish",
                    "cost": cost,
                    "tokens": {
                        "input": input_tokens,
                        "output": output_tokens,
                        "reasoning": 0,
                        "cache": {"read": cached, "write": 0},
                    },
                },
            }
        )
    )
    return "\n".join(lines) + "\n"


def test_builds_the_cli_command_and_sends_the_prompt(tmp_path: Path) -> None:
    runner = RecordingRunner(_Result(event_stream(["pong"])))
    provider = OpenCodeProvider(runner=runner, cwd=tmp_path, agent="plan")

    provider.complete(
        [
            Message("system", "rules here"),
            Message("user", "hello"),
            Message("assistant", "hi"),
            Message("user", "again"),
        ],
        "opencode-go/deepseek-v4.1-flash",
        128,
    )

    command, prompt, timeout, cwd = runner.calls[0]
    assert command == [
        "opencode-cli",
        "run",
        "--format",
        "json",
        "--archived",
        "--model",
        "opencode-go/deepseek-v4.1-flash",
        "--agent",
        "plan",
    ]
    assert "<alice_instructions>\nrules here\n</alice_instructions>" in prompt
    assert "user: hello" in prompt
    assert "assistant: hi" in prompt
    assert "user: again" in prompt
    assert timeout == DEFAULT_TIMEOUT
    assert cwd == str(tmp_path)


def test_parses_text_tokens_and_cost() -> None:
    completion = parse_events(
        event_stream(["hello", "world"], input_tokens=500, output_tokens=20, cached=100, cost=0.004)
    )

    assert completion.text == "hello\nworld"
    assert completion.input_tokens == 500
    assert completion.output_tokens == 20
    assert completion.cached_tokens == 100
    assert completion.cost_usd == pytest.approx(0.004)


def test_ignores_non_json_noise_and_unknown_events() -> None:
    stdout = "warning: something\n" + json.dumps({"type": "step_start"}) + "\n" + event_stream(["ok"])

    assert parse_events(stdout).text == "ok"


def test_missing_usage_is_an_error() -> None:
    stdout = json.dumps({"type": "text", "part": {"text": "hi"}}) + "\n"

    with pytest.raises(ProviderError, match="step_finish"):
        parse_events(stdout)


def test_empty_text_is_an_error() -> None:
    with pytest.raises(ProviderError, match="no text"):
        parse_events(event_stream([]))


def test_nonzero_exit_is_reported() -> None:
    runner = RecordingRunner(_Result(stderr="rate limit exceeded", returncode=1))
    provider = OpenCodeProvider(runner=runner)

    with pytest.raises(ProviderError) as caught:
        provider.complete([Message("user", "hi")], "model", 10)

    assert caught.value.transient is True
    assert "exited with code 1" in str(caught.value)
    assert "rate limit" in str(caught.value)


def test_timeout_is_transient() -> None:
    runner = RecordingRunner(subprocess.TimeoutExpired(["opencode-cli"], 1))
    provider = OpenCodeProvider(runner=runner, timeout=1.0)

    with pytest.raises(ProviderError) as caught:
        provider.complete([Message("user", "hi")], "model", 10)

    assert caught.value.transient is True


def test_missing_cli_is_loud(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("alice.models.opencode.find_cli", lambda: None)

    with pytest.raises(ProviderNotConfigured, match="OPENCODE_CLI"):
        OpenCodeProvider()


def test_find_cli_honours_the_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = tmp_path / "opencode-cli.exe"
    fake.write_bytes(b"")
    monkeypatch.setenv("OPENCODE_CLI", str(fake))

    assert find_cli() == str(fake)


def test_flatten_messages_without_system() -> None:
    prompt = flatten_messages([Message("user", "only this")])

    assert prompt == "<conversation>\nuser: only this\n</conversation>"


def test_router_accepts_a_reported_cost_provider(tmp_path: Path) -> None:
    db_path = tmp_path / "alice.db"
    init_db(db_path)
    conn = connect(db_path, create=False)
    try:
        provider = OpenCodeProvider(
            runner=RecordingRunner(_Result(event_stream(["ok"], input_tokens=10, output_tokens=2, cost=0.0)))
        )
        router = Router(
            models={"triage": ModelSpec(provider="opencode", model="opencode-go/x")},
            providers={"opencode": provider},
            usage=UsageLog(conn),
            budget=BudgetGuard(conn, Budget(soft_daily_usd=1.0, hard_daily_usd=2.0), tmp_path / "state"),
            sleep=lambda _seconds: None,
        )

        completion = router.complete("triage", [Message("user", "hi")], 10)

        assert completion.cost_usd == 0.0
        row = conn.execute("SELECT * FROM usage").fetchone()
        assert row["tier"] == "triage"
        assert row["model"] == "opencode-go/x"
        assert row["input_tokens"] == 10
        assert row["cost_usd"] == 0.0
    finally:
        conn.close()

