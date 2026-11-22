from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from alice.core.db import connect, init_db, utc_now
from alice.memory.archive import Archive
from alice.memory.config import EmbeddingConfig, MemoryConfig


class FakeEmbedder:
    dimension = 4

    def embed(self, texts):
        vectors = []
        for text in texts:
            lowered = text.lower()
            if "deploy" in lowered or "배포" in text:
                vectors.append([1.0, 0.0, 0.0, 0.0])
            elif "disk" in lowered or "디스크" in text or "storage" in lowered:
                vectors.append([0.0, 1.0, 0.0, 0.0])
            else:
                vectors.append([0.0, 0.0, 1.0, 0.0])
        return vectors


@pytest.fixture
def conn(tmp_path: Path):
    db = tmp_path / "alice.db"
    init_db(db)
    connection = connect(db, create=False)
    yield connection
    connection.close()


def add_event(conn: sqlite3.Connection, text: str, *, ts: str | None = None, digest: str | None = None) -> int:
    cursor = conn.execute(
        "INSERT INTO events (ts, source, kind, content_hash, text_redacted) "
        "VALUES (?, 'logfile:app.log', 'log', ?, ?)",
        (ts or utc_now(), digest or f"h{abs(hash(text))}", text),
    )
    conn.commit()
    return int(cursor.lastrowid)


def add_summary(conn: sqlite3.Connection, text: str, *, start: str = "2026-11-20T00:00:00Z") -> int:
    cursor = conn.execute(
        "INSERT INTO summaries (level, period_start, period_end, text) VALUES (?, ?, ?, ?)",
        ("day", start, "2026-11-21T00:00:00Z", text),
    )
    conn.commit()
    return int(cursor.lastrowid)


def config(tmp_path: Path, **kwargs) -> MemoryConfig:
    return MemoryConfig(
        core_memory_file=tmp_path / "core.json",
        recall_budget_tokens=kwargs.pop("recall_budget_tokens", 1000),
        embedding=kwargs.pop("embedding", None),
    )


def test_fts_recall_korean_and_english(conn, tmp_path: Path) -> None:
    add_event(conn, "배포 실패: 로그를 확인하세요")
    add_event(conn, "disk usage high")
    archive = Archive(conn, config(tmp_path))

    korean = archive.recall("배포")
    english = archive.recall("disk")

    assert any("배포" in item.text for item in korean.items)
    assert any("disk" in item.text for item in english.items)
    assert korean.truncated is False


def test_summaries_are_preferred(conn, tmp_path: Path) -> None:
    add_summary(conn, "deploy failed at noon")
    add_event(conn, "deploy failed at noon")
    archive = Archive(conn, config(tmp_path))

    result = archive.recall("deploy")

    assert result.items[0].kind == "summary"
    assert result.items[0].text == "deploy failed at noon"


def test_k_and_since_limits(conn, tmp_path: Path) -> None:
    add_event(conn, "deploy one", ts="2026-11-19T00:00:00Z")
    add_event(conn, "deploy two", ts="2026-11-21T00:00:00Z")
    archive = Archive(conn, config(tmp_path))

    assert len(archive.recall("deploy", k=1).items) == 1
    recent = archive.recall("deploy", since="2026-11-20T00:00:00Z")
    assert [item.text for item in recent.items] == ["deploy two"]


def test_token_budget_marks_truncation(conn, tmp_path: Path) -> None:
    add_event(conn, "deploy " + "x" * 400)
    archive = Archive(conn, config(tmp_path, recall_budget_tokens=5))

    result = archive.recall("deploy")

    assert result.items == ()
    assert result.truncated is True


def test_vector_index_and_recall(conn, tmp_path: Path) -> None:
    add_event(conn, "disk full on C:")
    add_event(conn, "deploy finished")
    cfg = MemoryConfig(
        core_memory_file=tmp_path / "core.json",
        embedding=EmbeddingConfig(model="fake", dimension=4),
    )
    archive = Archive(conn, cfg, embedder=FakeEmbedder())

    assert archive.vec_enabled
    indexed = archive.index_pending()
    assert indexed == 2
    assert conn.execute("SELECT COUNT(*) FROM memory_embeddings").fetchone()[0] == 2

    result = archive.recall("storage")

    assert result.items
    assert "disk" in result.items[0].text


def test_index_pending_without_embedder_is_a_noop(conn, tmp_path: Path) -> None:
    add_event(conn, "disk full")
    archive = Archive(conn, config(tmp_path))

    assert archive.index_pending() == 0
    assert conn.execute("SELECT COUNT(*) FROM memory_embeddings").fetchone()[0] == 0
