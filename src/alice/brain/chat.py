"""Local chat over Alice's ledger, memory, suggestions, and digest.

Chat is read-only. It can explain, correlate, and propose user steps, but it has
no tools and cannot write memory, edit files, send messages, or run commands.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Sequence

from ..contracts.memory import MemoryStore
from ..contracts.models import Message, ModelRouter
from ..contracts.tools import ToolExecutor
from ..core.db import utc_now
from .json_output import OutputFormatError, extract_object
from .prompts import build_chat_messages

logger = logging.getLogger(__name__)

MAX_QUESTION_CHARS = 4000
MAX_CHAT_TOKENS = 900
MAX_HISTORY_TURNS = 6
MAX_CONTEXT_CHARS = 14_000


class ChatError(RuntimeError):
    """Chat could not produce an answer."""


@dataclass(frozen=True)
class ChatMessage:
    id: int
    created: str
    role: str
    content: str
    citations: tuple[dict, ...] = ()
    error: str | None = None


@dataclass(frozen=True)
class ChatResult:
    user: ChatMessage
    assistant: ChatMessage
    citations: tuple[dict, ...]
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0


class ChatService:
    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        router: ModelRouter,
        memory: MemoryStore,
        hard_memory: Callable[[], str],
        data_dir: Path | str | None = None,
        max_tokens: int = MAX_CHAT_TOKENS,
        tools: ToolExecutor | None = None,
        growth_memory: Callable[[], str] | None = None,
    ) -> None:
        self.conn = conn
        self.router = router
        self.memory = memory
        self.hard_memory = hard_memory
        self.data_dir = Path(data_dir) if data_dir is not None else None
        self.max_tokens = max_tokens
        self.tools = tools
        self.growth_memory = growth_memory

    def history(self, limit: int = 100) -> tuple[ChatMessage, ...]:
        return load_chat_history(self.conn, limit)

    def clear(self) -> None:
        self.conn.execute("DELETE FROM chat_messages")
        self.conn.commit()

    def ask(
        self,
        question: str,
        *,
        attachments: Sequence[tuple[str, str]] = (),
    ) -> ChatResult:
        text = question.strip()
        if not text:
            raise ChatError("Ask a question first.")
        if len(text) > MAX_QUESTION_CHARS:
            raise ChatError(f"Question is too long (max {MAX_QUESTION_CHARS} characters).")

        prior = self.history(MAX_HISTORY_TURNS) if _has_table(self.conn, "chat_messages") else ()
        user = self._save("user", text, ())

        context_data = self._build_context(text, attachments)
        specs = self.tools.specs() if self.tools is not None else ()
        messages, prefix_len = build_chat_messages(
            text,
            context=self._format_context(context_data),
            history=[(message.role, message.content) for message in prior],
            tools=specs,
        )

        completion = None
        answer: str | None = None
        try:
            for _ in range(5):
                completion = self.router.complete(
                    "reason",
                    messages,
                    self.max_tokens,
                    purpose="chat",
                    cache_prefix_len=prefix_len,
                )
                request = _tool_request(completion.text)
                if (
                    self.tools is not None
                    and request is not None
                    and request.get("action") == "tool"
                ):
                    name = str(request.get("tool") or "").strip()
                    args = request.get("args") or {}
                    if not isinstance(args, dict):
                        args = {}
                    result = self.tools.execute(name, args)
                    messages.append(Message("assistant", completion.text.strip()))
                    messages.append(
                        Message(
                            "system",
                            f'<tool_result tool="{name}" ok="{str(result.ok).lower()}">\n'
                            f"{result.text[:8000]}\n"
                            "</tool_result>",
                        )
                    )
                    continue
                if request is not None and request.get("action") == "answer":
                    value = request.get("text")
                    if isinstance(value, str) and value.strip():
                        answer = value.strip()
                if answer is None:
                    answer = completion.text.strip()
                break
            if answer is None:
                messages.append(
                    Message(
                        "system",
                        "You have enough tool results. Do not call another tool. "
                        'Return the final answer as {"action":"answer","text":"..."}.',
                    )
                )
                completion = self.router.complete(
                    "reason",
                    messages,
                    self.max_tokens,
                    purpose="chat-final",
                    cache_prefix_len=prefix_len,
                )
                request = _tool_request(completion.text)
                value = request.get("text") if request else None
                answer = (
                    value.strip()
                    if isinstance(value, str) and value.strip()
                    else completion.text.strip()
                )
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            logger.warning("chat call failed: %s", error)
            assistant = self._save(
                "assistant",
                f"I could not answer: {error}",
                (),
                error=error,
            )
            return ChatResult(user=user, assistant=assistant, citations=())

        citations = _citations(context_data)
        assistant = self._save(
            "assistant",
            answer or "I could not settle on an answer.",
            citations,
            input_tokens=completion.input_tokens if completion else 0,
            output_tokens=completion.output_tokens if completion else 0,
            cost_usd=completion.cost_usd if completion else 0.0,
        )
        return ChatResult(
            user=user,
            assistant=assistant,
            citations=citations,
            input_tokens=completion.input_tokens if completion else 0,
            output_tokens=completion.output_tokens if completion else 0,
            cost_usd=completion.cost_usd if completion else 0.0,
        )

    # -- context -----------------------------------------------------------

    def _build_context(
        self, question: str, attachments: Sequence[tuple[str, str]] = ()
    ) -> dict:
        hard = ""
        try:
            hard = self.hard_memory()
        except Exception:
            logger.exception("chat could not read hard memory")

        growth = ""
        if self.growth_memory is not None:
            try:
                growth = self.growth_memory()
            except Exception:
                logger.exception("chat could not read growth memory")

        recalled = ()
        try:
            recalled = self.memory.recall(question, k=5).items
        except Exception:
            logger.exception("chat recall failed")

        suggestions = self.conn.execute(
            """
            SELECT id, priority, status, text, reason, steps, evidence,
                   category, confidence
            FROM suggestions
            WHERE status IN ('queued', 'shown')
            ORDER BY priority, id DESC
            LIMIT 8
            """
        ).fetchall()
        events = self.conn.execute(
            """
            SELECT id, ts, source, status, salience, text_redacted
            FROM events
            WHERE status IN ('new', 'escalated') AND salience >= 0.5
            ORDER BY salience DESC, id DESC
            LIMIT 10
            """
        ).fetchall()

        digest = ""
        if self.data_dir is not None:
            day = datetime.now().astimezone().date().isoformat()
            path = self.data_dir / "digests" / f"{day}.md"
            if path.is_file():
                try:
                    digest = path.read_text(encoding="utf-8")[-6000:]
                except OSError:
                    digest = ""

        return {
            "hard": hard,
            "growth": growth,
            "recalled": recalled,
            "suggestions": suggestions,
            "events": events,
            "digest": digest,
            "attachments": tuple(attachments),
        }

    def _format_context(self, context: dict) -> str:
        parts: list[str] = []
        hard = str(context.get("hard") or "").strip()
        if hard:
            parts.append(f"<hard_memory>\n{hard}\n</hard_memory>")
        growth = str(context.get("growth") or "").strip()
        if growth:
            parts.append(f"<growth_memory>\n{growth}\n</growth_memory>")
        recalled = context.get("recalled") or ()
        if recalled:
            lines = ["<retrieved_memory>"]
            for item in recalled:
                lines.append(
                    f"- kind={item.kind} key={item.key or '-'} "
                    f"score={item.score:.2f}: {item.text}"
                )
            lines.append("</retrieved_memory>")
            parts.append("\n".join(lines))
        suggestions = context.get("suggestions") or ()
        if suggestions:
            lines = ["<open_suggestions>"]
            for row in suggestions:
                lines.append(
                    f"- id={row['id']} P{row['priority']} status={row['status']} "
                    f"category={row['category'] or '-'}: {row['text']}"
                )
                if row["reason"]:
                    lines.append(f"  reason: {row['reason']}")
                if row["steps"]:
                    try:
                        steps = json.loads(row["steps"] or "[]")
                    except (TypeError, ValueError):
                        steps = []
                    for step in steps:
                        lines.append(f"  step: {step}")
            lines.append("</open_suggestions>")
            parts.append("\n".join(lines))
        events = context.get("events") or ()
        if events:
            lines = ["<recent_events>"]
            for row in events:
                lines.append(
                    f"- id={row['id']} ts={row['ts']} source={row['source']} "
                    f"status={row['status']} salience={row['salience']}: "
                    f"{row['text_redacted'][:400]}"
                )
            lines.append("</recent_events>")
            parts.append("\n".join(lines))
        attachments = context.get("attachments") or ()
        if attachments:
            lines = ["<attached_files>"]
            for label, content in attachments:
                lines.append(f"<file path=\"{label}\">")
                lines.append(content)
                lines.append("</file>")
            lines.append("</attached_files>")
            parts.append("\n".join(lines))
        digest = str(context.get("digest") or "").strip()
        if digest:
            parts.append(f"<today_digest>\n{digest}\n</today_digest>")
        text = "\n\n".join(parts)
        if len(text) > MAX_CONTEXT_CHARS:
            text = text[:MAX_CONTEXT_CHARS] + "\n...[context truncated]"
        return text

    def _citations(self, context: dict) -> tuple[dict, ...]:  # type: ignore[override]
        return _citations(context)

    def _save(
        self,
        role: str,
        content: str,
        citations: Sequence[dict],
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cost_usd: float = 0.0,
        error: str | None = None,
    ) -> ChatMessage:
        created = utc_now()
        cursor = self.conn.execute(
            """
            INSERT INTO chat_messages
              (created, role, content, citations, input_tokens, output_tokens,
               cost_usd, error)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                created,
                role,
                content,
                json.dumps(list(citations)),
                int(input_tokens),
                int(output_tokens),
                float(cost_usd),
                error,
            ),
        )
        self.conn.commit()
        return ChatMessage(
            id=int(cursor.lastrowid),
            created=created,
            role=role,
            content=content,
            citations=tuple(citations),
            error=error,
        )


def _citations(context: dict) -> tuple[dict, ...]:
    citations: list[dict] = []
    for item in context.get("recalled", ()):
        citations.append(
            {
                "kind": "memory",
                "id": item.key or None,
                "label": f"{item.kind}: {item.text[:120]}",
            }
        )
    for row in context.get("suggestions", ()):
        citations.append(
            {
                "kind": "suggestion",
                "id": int(row["id"]),
                "label": f"P{row['priority']} {row['text'][:120]}",
            }
        )
    for row in context.get("events", ()):
        citations.append(
            {
                "kind": "event",
                "id": int(row["id"]),
                "label": f"{row['source']}: {row['text_redacted'][:120]}",
            }
        )
    for label, _ in context.get("attachments", ()):
        citations.append(
            {
                "kind": "file",
                "id": label,
                "label": f"file: {label}",
            }
        )
    return tuple(citations)


def load_chat_history(
    conn: sqlite3.Connection, limit: int = 100
) -> tuple[ChatMessage, ...]:
    if not _has_table(conn, "chat_messages"):
        return ()
    rows = conn.execute(
        "SELECT * FROM chat_messages ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    return tuple(_row(row) for row in reversed(rows))


def _row(row: sqlite3.Row) -> ChatMessage:
    try:
        citations = tuple(json.loads(row["citations"] or "[]"))
    except (TypeError, ValueError, json.JSONDecodeError):
        citations = ()
    return ChatMessage(
        id=int(row["id"]),
        created=str(row["created"]),
        role=str(row["role"]),
        content=str(row["content"]),
        citations=citations,
        error=row["error"],
    )


def _tool_request(text: str) -> dict | None:
    try:
        payload = extract_object(text)
    except OutputFormatError:
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("action") not in ("tool", "answer"):
        return None
    return payload


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone()
    return row is not None
