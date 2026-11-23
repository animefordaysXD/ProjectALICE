from __future__ import annotations

import json
from pathlib import Path

from alice.brain.explore import ExplorationService
from alice.contracts.models import Completion
from alice.contracts.tools import ToolResult


class StubRouter:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls = 0

    def complete(self, tier, messages, max_tokens, *, purpose=None, cache_prefix_len=None):
        self.calls += 1
        if self.fail:
            raise RuntimeError("provider offline")
        if purpose == "exploration-summary":
            return Completion(
                text="# Session summary\n\nAlice read a few pages and took notes.",
                input_tokens=60,
                output_tokens=20,
                cost_usd=0.0,
            )
        return Completion(
            text=json.dumps(
                {
                    "note": "This page is about a useful topic. The references look promising.",
                    "next": "https://en.wikipedia.org/wiki/Next_Page",
                    "why": "it continues the topic",
                }
            ),
            input_tokens=100,
            output_tokens=30,
            cost_usd=0.0,
        )


class StubTools:
    def __init__(self) -> None:
        self.calls = 0

    def specs(self):
        return ()

    def execute(self, name, args=None):
        self.calls += 1
        url = str((args or {}).get("url") or "")
        if "Next_Page" in url:
            return ToolResult(
                True,
                "Next Page\n\nA page about the next step.",
                {
                    "title": "Next Page",
                    "url": url,
                    "links": [
                        "https://en.wikipedia.org/wiki/Third_Page",
                        "https://en.wikipedia.org/wiki/File:Ignored",
                    ],
                },
            )
        return ToolResult(
            True,
            "Start Page\n\nA page about beginnings.",
            {
                "title": "Start Page",
                "url": url or "https://en.wikipedia.org/wiki/Main_Page",
                "links": ["https://en.wikipedia.org/wiki/Next_Page"],
            },
        )


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def test_exploration_writes_journal_and_summary(tmp_path: Path) -> None:
    clock = Clock()
    router = StubRouter()
    tools = StubTools()
    service = ExplorationService(router, tools, tmp_path)

    result = service.run(
        minutes=0.05,
        step_seconds=1,
        max_steps=3,
        sleep=clock.sleep,
        monotonic=clock,
    )

    assert len(result.steps) == 3
    assert result.summary_path.is_file()
    assert result.journal_path.is_file()
    assert "Session summary" in result.summary_path.read_text(encoding="utf-8")
    assert result.steps[0].next_url == "https://en.wikipedia.org/wiki/Next_Page"
    assert result.stop_reason == "time budget reached"
    latest = json.loads((tmp_path / "exploration" / "latest.json").read_text("utf-8"))
    assert latest["status"] == "completed"
    assert latest["steps"] == 3
    assert latest["summary_path"].endswith("summary.md")
    assert tools.calls == 3


def test_exploration_survives_model_failure(tmp_path: Path) -> None:
    clock = Clock()
    service = ExplorationService(StubRouter(fail=True), StubTools(), tmp_path)

    result = service.run(
        minutes=1,
        step_seconds=0,
        max_steps=10,
        sleep=clock.sleep,
        monotonic=clock,
    )

    assert len(result.steps) == 3
    assert "model unavailable" in result.stop_reason
    assert "Exploration summary" in result.summary
    assert result.summary_path.is_file()
