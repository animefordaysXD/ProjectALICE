"""Anthropic Messages API adapter.

The client and SDK are imported lazily so starting Alice (or running tests)
does not pay for a large dependency that may never be used. The key is never
stored on the instance, logged, or written to the database.
"""

from __future__ import annotations

import os
from typing import Any, Sequence

from .base import Completion, Message, Provider, ProviderError, ProviderNotConfigured

DEFAULT_TIMEOUT = 60.0

_TRANSIENT_ERROR_NAMES = {
    "RateLimitError",
    "APITimeoutError",
    "APIConnectionError",
    "InternalServerError",
    "OverloadedError",
}
_CACHE_CONTROL = {"type": "ephemeral"}


class AnthropicProvider(Provider):
    name = "anthropic"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        client: Any | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        if client is None:
            try:
                import anthropic
            except ImportError as exc:
                raise ProviderNotConfigured(
                    "the anthropic package is not installed"
                ) from exc
            key = api_key or os.environ.get("ANTHROPIC_API_KEY")
            if not key:
                raise ProviderNotConfigured(
                    "ANTHROPIC_API_KEY is not set; put it in the environment or "
                    "Windows Credential Manager"
                )
            client = anthropic.Anthropic(api_key=key, timeout=timeout)
        self._client = client

    def complete(
        self,
        messages: Sequence[Message],
        model: str,
        max_tokens: int,
        cache_prefix_len: int | None = None,
    ) -> Completion:
        if max_tokens <= 0:
            raise ValueError("max_tokens must be positive")

        system, chat = build_payload(messages, cache_prefix_len)
        payload: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": chat,
        }
        if system is not None:
            payload["system"] = system

        try:
            response = self._client.messages.create(**payload)
        except Exception as exc:
            raise translate_error(exc) from exc

        text = "".join(
            block.text
            for block in response.content
            if getattr(block, "type", "text") == "text" and getattr(block, "text", None)
        )
        usage = response.usage
        return Completion(
            text=text,
            input_tokens=int(usage.input_tokens),
            output_tokens=int(usage.output_tokens),
            cached_tokens=int(getattr(usage, "cache_read_input_tokens", 0) or 0),
        )


def build_payload(
    messages: Sequence[Message], cache_prefix_len: int | None = None
) -> tuple[Any | None, list[dict[str, Any]]]:
    """Convert messages to Anthropic's shape, applying one cache breakpoint.

    `cache_prefix_len` counts characters from the start of the prompt (system
    text first, then message contents). The block containing the boundary is
    split and its first half is marked cacheable. A zero/None prefix sends no
    cache control.
    """
    system_text = "\n\n".join(m.content for m in messages if m.role == "system")
    system: Any | None = system_text or None
    chat: list[dict[str, Any]] = [
        {"role": m.role, "content": [{"type": "text", "text": m.content}]}
        for m in messages
        if m.role != "system"
    ]
    if not chat:
        raise ValueError("at least one user or assistant message is required")
    if cache_prefix_len is None or cache_prefix_len <= 0:
        return system, chat

    remaining = cache_prefix_len
    if system_text:
        if remaining < len(system_text):
            system = [
                _cached(system_text[:remaining]),
                {"type": "text", "text": system_text[remaining:]},
            ]
            return system, chat
        remaining -= len(system_text)
        system = [_cached(system_text)]
        if remaining == 0:
            return system, chat

    for message in chat:
        text = message["content"][0]["text"]
        if remaining < len(text):
            message["content"] = [
                _cached(text[:remaining]),
                {"type": "text", "text": text[remaining:]},
            ]
            return system, chat
        remaining -= len(text)
        if remaining == 0:
            message["content"][0]["cache_control"] = _CACHE_CONTROL
            return system, chat

    # Prefix longer than the whole prompt: cache everything sent.
    chat[-1]["content"][0]["cache_control"] = _CACHE_CONTROL
    return system, chat


def _cached(text: str) -> dict[str, Any]:
    return {"type": "text", "text": text, "cache_control": _CACHE_CONTROL}


def translate_error(exc: Exception) -> ProviderError:
    """Map SDK errors to a retryable flag without echoing request content."""
    name = type(exc).__name__
    status = getattr(exc, "status_code", None)
    transient = (
        name in _TRANSIENT_ERROR_NAMES
        or status == 429
        or (isinstance(status, int) and status >= 500)
    )
    return ProviderError(f"anthropic call failed: {name}", transient=transient)
