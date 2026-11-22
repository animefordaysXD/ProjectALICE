"""Deterministic provider used by every test and by `--dry-run` later."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from .base import Completion, Message, Provider, ProviderError


@dataclass(frozen=True)
class FakeCall:
    messages: tuple[Message, ...]
    model: str
    max_tokens: int
    cache_prefix_len: int | None


def _tokens(text: str) -> int:
    return max(1, len(text) // 4)


class FakeProvider(Provider):
    """Canned responses, deterministic token counts, no network.

    `calls` records every request so tests can scan outgoing payloads.
    `fail_times` simulates transient failures for retry tests.
    """

    name = "fake"
    reports_cost = True

    def __init__(
        self,
        responses: Sequence[str] | None = None,
        *,
        fail_times: int = 0,
        default_response: str = "fake ok",
    ) -> None:
        self._responses = list(responses or [])
        self._default = default_response
        self.fail_times = fail_times
        self.calls: list[FakeCall] = []

    def complete(
        self,
        messages: Sequence[Message],
        model: str,
        max_tokens: int,
        cache_prefix_len: int | None = None,
    ) -> Completion:
        if max_tokens <= 0:
            raise ValueError("max_tokens must be positive")
        self.calls.append(
            FakeCall(
                messages=tuple(messages),
                model=model,
                max_tokens=max_tokens,
                cache_prefix_len=cache_prefix_len,
            )
        )
        if self.fail_times > 0:
            self.fail_times -= 1
            raise ProviderError("simulated transient failure", transient=True)

        text = self._responses.pop(0) if self._responses else self._default
        prompt = "".join(message.content for message in messages)
        return Completion(
            text=text,
            input_tokens=_tokens(prompt),
            output_tokens=_tokens(text),
        )

    def reset(self) -> None:
        self.calls.clear()
