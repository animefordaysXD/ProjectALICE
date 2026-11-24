from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from alice.app.accessibility import AccessibilityContext
from alice.app.activity import Activity
from alice.app.monitor import ScreenMonitor, _scrub_screen, detect_scenario
from alice.app.monitor_config import MonitorConfig
from alice.app.screen import ScreenText
from alice.contracts.models import Completion
from alice.core.db import connect, init_db
from alice.outputs.inbox import NotificationCenter
from alice.outputs.queue import SuggestionQueue


class StubRouter:
    def __init__(self, payload=None, *, fail: bool = False):
        self.payload = payload or {
            "suggestions": [
                {
                    "text": "You have been editing one file for a while; save a checkpoint.",
                    "reason": "A cheap safety step",
                    "steps": ["Save a copy", "Continue"],
                    "category": "workflow",
                    "confidence": 0.8,
                    "priority": 2,
                    "cluster": "save-checkpoint",
                }
            ]
        }
        self.fail = fail
        self.calls = 0

    def complete(self, tier, messages, max_tokens, *, purpose=None, cache_prefix_len=None):
        self.calls += 1
        if self.fail:
            raise RuntimeError("provider offline")
        return Completion(
            text=json.dumps(self.payload),
            input_tokens=100,
            output_tokens=20,
            cost_usd=0.0,
        )


@pytest.fixture
def conn(tmp_path: Path):
    db = tmp_path / "alice.db"
    init_db(db)
    connection = connect(db, create=False)
    yield connection
    connection.close()


def activity(category: str = "coding", app: str = "code.exe", idle: int = 0) -> Activity:
    return Activity(
        app=app,
        title="notes.txt - editor",
        category=category,
        idle_seconds=idle,
        captured_at="2026-11-24T14:00:00+09:00",
    )


def capture(text: str = "Project notes\nRemember to save the file.") -> ScreenText:
    return ScreenText(text=text, width=100, height=100, digest="x")


def monitor(
    conn,
    router=None,
    *,
    capture_fn=None,
    activity_fn=None,
    accessibility_fn=None,
    growth=None,
    **kwargs,
):
    config = MonitorConfig(
        capture_seconds=0,
        analyze_seconds=0,
        idle_seconds=180,
        max_ocr_chars=1000,
        min_confidence=0.5,
        max_suggestions_per_hour=2,
        exclude_apps=("keepass.exe",),
        exclude_titles=("password",),
        excerpt_chars=120,
    )
    for key, value in kwargs.items():
        object.__setattr__(config, key, value)
    return ScreenMonitor(
        conn,
        router or StubRouter(),
        SuggestionQueue(conn),
        config,
        Path(conn.execute("PRAGMA database_list").fetchone()[2]).parent,
        capture=capture_fn or (lambda: capture()),
        activity=activity_fn or (lambda: activity()),
        accessibility=accessibility_fn or (lambda: None),
        growth=growth,
    )


def test_screen_suggestion_creates_in_app_ping(conn) -> None:
    router = StubRouter()
    service = monitor(conn, router)
    now = datetime(2026, 11, 24, 14, 0).astimezone()

    ids = service.tick(now)

    assert len(ids) == 1
    pending = SuggestionQueue(conn).pending()
    assert pending[0].category == "workflow"
    assert "checkpoint" in pending[0].text
    notifications = NotificationCenter(conn).unread()
    assert len(notifications) == 1
    assert notifications[0].suggestion_id == ids[0]
    assert notifications[0].read is False


def test_unchanged_screen_does_not_reanalyze(conn) -> None:
    router = StubRouter()
    service = monitor(conn, router)
    now = datetime(2026, 11, 24, 14, 0).astimezone()

    service.tick(now)
    service.tick(now + timedelta(seconds=30))

    assert router.calls == 1


def test_idle_and_excluded_apps_are_skipped(conn) -> None:
    captured = []
    router = StubRouter()
    service = monitor(
        conn,
        router,
        capture_fn=lambda: captured.append(1) or capture(),
        activity_fn=lambda: activity(idle=300),
    )
    assert service.tick(datetime(2026, 11, 24, 14, 0).astimezone()) == ()
    assert captured == []

    service2 = monitor(
        conn,
        router,
        capture_fn=lambda: captured.append(1) or capture(),
        activity_fn=lambda: activity(app="keepass.exe"),
    )
    assert service2.tick(datetime(2026, 11, 24, 14, 0).astimezone()) == ()
    assert captured == []


def test_low_confidence_is_dropped(conn) -> None:
    router = StubRouter(
        {
            "suggestions": [
                {
                    "text": "Maybe do something.",
                    "category": "other",
                    "confidence": 0.1,
                    "priority": 2,
                    "cluster": "weak",
                }
            ]
        }
    )
    service = monitor(conn, router)
    assert service.tick(datetime(2026, 11, 24, 14, 0).astimezone()) == ()
    assert SuggestionQueue(conn).pending() == []


def test_model_failure_is_contained(conn) -> None:
    service = monitor(conn, StubRouter(fail=True))
    assert service.tick(datetime(2026, 11, 24, 14, 0).astimezone()) == ()
    assert SuggestionQueue(conn).pending() == []


def test_credential_lines_are_scrubbed() -> None:
    text = "Project notes\npassword: hunter2\ntoken = abc123\nContinue working"
    assert "hunter2" not in _scrub_screen(text)
    assert "abc123" not in _scrub_screen(text)
    assert "Project notes" in _scrub_screen(text)


def test_monitor_state_is_local(tmp_path: Path, conn) -> None:
    service = monitor(conn)
    now = datetime(2026, 11, 24, 14, 0).astimezone()
    service.tick(now)
    state = json.loads((service.data_dir / "monitor_state.json").read_text("utf-8"))
    assert state["status"] == "analyzed"
    assert state["app"] == "code.exe"
    assert "excerpt" in state


def test_question_becomes_in_app_ping(conn) -> None:
    router = StubRouter({"suggestions": [], "question": "Is this a review or a rewrite?"})
    service = monitor(conn, router)
    ids = service.tick(datetime(2026, 11, 24, 14, 0).astimezone())
    assert len(ids) == 1
    pending = SuggestionQueue(conn).pending()
    assert pending[0].category == "question"
    assert pending[0].text.endswith("?")


class StubGrowth:
    def __init__(self) -> None:
        self.observation = None
        self.rendered = 0

    def render(self) -> str:
        self.rendered += 1
        return "[GROWTH MEMORY]\n- (0.20, interest) A growing interest in interfaces."

    def ingest_observation(self, **kwargs):
        self.observation = kwargs
        return 1


def test_screen_observation_feeds_growth_memory(conn) -> None:
    router = StubRouter(
        {
            "suggestions": [],
            "observation": "Jo is comparing two interface layouts.",
            "growth_notes": [
                {
                    "key": "interface-design",
                    "kind": "interest",
                    "text": "A growing interest in interface design trade-offs.",
                    "confidence": 0.45,
                }
            ],
        }
    )
    growth = StubGrowth()
    service = monitor(conn, router, growth=growth)
    service.tick(datetime(2026, 11, 24, 14, 0).astimezone())
    assert growth.rendered == 1
    assert growth.observation is not None
    assert growth.observation["summary"] == "Jo is comparing two interface layouts."
    assert growth.observation["notes"][0]["key"] == "interface-design"


class CapturingRouter(StubRouter):
    def __init__(self):
        super().__init__({"suggestions": [], "question": "What is the goal here?"})
        self.messages = []

    def complete(self, tier, messages, max_tokens, *, purpose=None, cache_prefix_len=None):
        self.messages = list(messages)
        return super().complete(
            tier,
            messages,
            max_tokens,
            purpose=purpose,
            cache_prefix_len=cache_prefix_len,
        )


def test_accessibility_context_is_added_to_the_prompt(conn) -> None:
    router = CapturingRouter()
    service = monitor(
        conn,
        router,
        accessibility_fn=lambda: AccessibilityContext(
            text='Document "Quarterly report"\nEdit "Budget" value="1200"',
            focused="Edit Budget",
            node_count=2,
            process_id=1,
        ),
    )
    service.tick(datetime(2026, 11, 24, 14, 0).astimezone())
    prompt = "\n".join(message.content for message in router.messages)
    assert "UI elements (bounded)" in prompt
    assert "Quarterly report" in prompt
    assert "Likely scenario" in prompt


def test_scenario_hints() -> None:
    assert detect_scenario("other", "Traceback: ValueError: bad input") == "error"
    assert detect_scenario("coding", "def main(): pass") == "coding"
    assert detect_scenario("writing", "A long document body") == "writing"
    assert detect_scenario("browsing", "word " * 400) == "reading"
    assert detect_scenario("calendar", "inbox") == "general"
