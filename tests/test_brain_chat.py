from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from alice.brain.chat import ChatError, ChatService, load_chat_history
from alice.contracts.memory import MemoryItem, Recall
from alice.contracts.models import Completion, Message
from alice.core.db import connect, init_db, utc_now


class StubRouter:
    def __init__(self, text: str = "Check the queue broker first.", *, fail: bool = False):
        self.text = text
        self.fail = fail
        self.calls: list[tuple[str, tuple[Message, ...]]] = []

    def complete(self, tier, messages, max_tokens, *, purpose=None, cache_prefix_len=None):
        self.calls.append((tier, tuple(messages)))
        if self.fail:
            raise RuntimeError("provider offline")
        return Completion(
            text=self.text,
            input_tokens=120,
            output_tokens=30,
            cost_usd=0.002,
        )


class StubMemory:
    def hard_block(self) -> str:
        return "Project: Alice\nOwner: Jo"

    def recall(self, query, k=5, since=None):
        return Recall(
            items=(
                MemoryItem(
                    kind="summary",
                    text="The queue broker restarted at 03:20.",
                    score=0.9,
                    key="2026-11-23T03:00:00Z",
                ),
            )
        )


@pytest.fixture
def conn(tmp_path: Path):
    db = tmp_path / "alice.db"
    init_db(db)
    connection = connect(db, create=False)
    connection.execute(
        "INSERT INTO suggestions (created, priority, text, reason, status, steps) "
        "VALUES (?, 1, 'Check the queue broker.', 'shared dependency', 'queued', ?)",
        (utc_now(), '["Open the broker dashboard", "Check consumer lag"]'),
    )
    connection.execute(
        "INSERT INTO events (ts, source, kind, content_hash, text_redacted, salience) "
        "VALUES (?, 'logfile:app.log', 'log', 'h1', 'flush timeout', 0.9)",
        (utc_now(),),
    )
    connection.commit()
    yield connection
    connection.close()


def service(conn: sqlite3.Connection, router=None, memory=None, **kwargs) -> ChatService:
    return ChatService(
        conn,
        router=router or StubRouter(),
        memory=memory or StubMemory(),
        hard_memory=(memory or StubMemory()).hard_block,
        **kwargs,
    )


def test_chat_persists_conversation_and_citations(conn) -> None:
    router = StubRouter()
    result = service(conn, router).ask("What should I check?")

    assert result.assistant.content == "Check the queue broker first."
    assert result.input_tokens == 120
    assert result.cost_usd == 0.002
    assert any(citation["kind"] == "suggestion" for citation in result.citations)
    assert any(citation["kind"] == "event" for citation in result.citations)
    assert any(citation["kind"] == "memory" for citation in result.citations)

    history = load_chat_history(conn)
    assert [message.role for message in history] == ["user", "assistant"]
    assert history[0].content == "What should I check?"

    tier, messages = router.calls[0]
    assert tier == "reason"
    joined = "\n".join(message.content for message in messages)
    assert "<local_context>" in joined
    assert "Project: Alice" in joined
    assert "Check the queue broker." in joined
    assert "flush timeout" in joined


def test_chat_includes_attached_files(conn) -> None:
    router = StubRouter("The file says hello.")
    result = service(conn, router).ask(
        "What does the attached file say?",
        attachments=[("notes.txt", "hello world")],
    )

    assert any(citation["kind"] == "file" for citation in result.citations)
    _tier, messages = router.calls[0]
    joined = "\n".join(message.content for message in messages)
    assert "<attached_files>" in joined
    assert "hello world" in joined


def test_chat_error_is_saved(conn) -> None:
    result = service(conn, StubRouter(fail=True)).ask("Are you there?")

    assert result.assistant.error
    assert "provider offline" in result.assistant.content
    history = load_chat_history(conn)
    assert len(history) == 2
    assert history[-1].role == "assistant"
    assert history[-1].error


def test_chat_rejects_empty_question(conn) -> None:
    with pytest.raises(ChatError):
        service(conn).ask("   ")


def test_chat_clear(conn) -> None:
    chat = service(conn)
    chat.ask("Hello")
    chat.clear()
    assert load_chat_history(conn) == ()
