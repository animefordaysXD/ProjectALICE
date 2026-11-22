from __future__ import annotations

import pytest

from alice.models.anthropic import (
    AnthropicProvider,
    build_payload,
    translate_error,
)
from alice.models.base import Message, ProviderError, ProviderNotConfigured


class _Usage:
    def __init__(self, input_tokens: int, output_tokens: int, cached: int = 0) -> None:
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.cache_read_input_tokens = cached


class _TextBlock:
    type = "text"

    def __init__(self, text: str) -> None:
        self.text = text


class _Response:
    def __init__(self, blocks: list[_TextBlock], usage: _Usage) -> None:
        self.content = blocks
        self.usage = usage


class _MessagesApi:
    def __init__(self, response=None, error: Exception | None = None) -> None:
        self.response = response or _Response([_TextBlock("ok")], _Usage(1, 1))
        self.error = error
        self.kwargs: dict | None = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        if self.error is not None:
            raise self.error
        return self.response


class _Client:
    def __init__(self, response=None, error: Exception | None = None) -> None:
        self.messages = _MessagesApi(response, error)


def test_complete_maps_text_and_tokens() -> None:
    response = _Response(
        [_TextBlock("hello "), _TextBlock("world")], _Usage(120, 30, cached=40)
    )
    client = _Client(response)
    provider = AnthropicProvider(client=client)

    result = provider.complete(
        [Message("system", "rules"), Message("user", "hi")],
        "model-x",
        64,
    )

    assert result.text == "hello world"
    assert result.input_tokens == 120
    assert result.output_tokens == 30
    assert result.cached_tokens == 40
    assert client.messages.kwargs == {
        "model": "model-x",
        "max_tokens": 64,
        "system": "rules",
        "messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
    }


def test_payload_without_cache_keeps_string_system() -> None:
    system, chat = build_payload(
        [Message("system", "stable"), Message("user", "task")]
    )

    assert system == "stable"
    assert chat == [{"role": "user", "content": [{"type": "text", "text": "task"}]}]


def test_cache_breakpoint_inside_system() -> None:
    system, _ = build_payload(
        [Message("system", "abcdef"), Message("user", "hi")], 3
    )

    assert system == [
        {"type": "text", "text": "abc", "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": "def"},
    ]


def test_cache_breakpoint_at_system_end() -> None:
    system, chat = build_payload(
        [Message("system", "abcdef"), Message("user", "hi")], 6
    )

    assert system == [
        {"type": "text", "text": "abcdef", "cache_control": {"type": "ephemeral"}}
    ]
    assert chat[0]["content"] == [{"type": "text", "text": "hi"}]


def test_cache_breakpoint_inside_user_message() -> None:
    system, chat = build_payload(
        [Message("system", "ab"), Message("user", "cdef")], 4
    )

    assert system == [
        {"type": "text", "text": "ab", "cache_control": {"type": "ephemeral"}}
    ]
    assert chat[0]["content"] == [
        {"type": "text", "text": "cd", "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": "ef"},
    ]


def test_cache_prefix_past_the_end_marks_last_block() -> None:
    _, chat = build_payload([Message("user", "abc")], 999)

    assert chat[0]["content"][0]["cache_control"] == {"type": "ephemeral"}


def test_system_only_payload_is_rejected() -> None:
    with pytest.raises(ValueError, match="user or assistant"):
        build_payload([Message("system", "only rules")])


class RateLimitError(Exception):
    status_code = 429


class APIConnectionError(Exception):
    pass


class BadRequestError(Exception):
    status_code = 400


def test_error_translation_marks_transient_errors() -> None:
    assert translate_error(RateLimitError()).transient is True
    assert translate_error(APIConnectionError()).transient is True
    assert translate_error(BadRequestError()).transient is False


def test_error_translation_does_not_echo_details() -> None:
    error = translate_error(RateLimitError("key sk-ant-secret and message text"))

    assert "sk-ant" not in str(error)
    assert "message text" not in str(error)


def test_client_errors_become_provider_errors() -> None:
    provider = AnthropicProvider(client=_Client(error=RateLimitError()))

    with pytest.raises(ProviderError) as caught:
        provider.complete([Message("user", "hi")], "model-x", 10)

    assert caught.value.transient is True


def test_missing_api_key_is_loud(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    with pytest.raises(ProviderNotConfigured, match="ANTHROPIC_API_KEY"):
        AnthropicProvider()


def test_zero_max_tokens_is_rejected() -> None:
    provider = AnthropicProvider(client=_Client())

    with pytest.raises(ValueError, match="max_tokens"):
        provider.complete([Message("user", "hi")], "model-x", 0)

