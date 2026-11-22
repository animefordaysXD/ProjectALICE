from __future__ import annotations

import re
from pathlib import Path

import pytest

from alice.core.db import connect, init_db
from alice.memory.config import MemoryConfig
from alice.memory.errors import MemoryCapError, ProposalError
from alice.memory.hard import HardMemory
from alice.memory.service import MemoryService

SRC = Path(__file__).resolve().parents[1] / "src" / "alice"


@pytest.fixture
def conn(tmp_path: Path):
    db = tmp_path / "alice.db"
    init_db(db)
    connection = connect(db, create=False)
    yield connection
    connection.close()


def test_proposal_does_not_write_hard_memory(conn) -> None:
    hard = HardMemory(conn)

    proposal_id = hard.propose("timezone", "Asia/Seoul", "observed")

    assert proposal_id > 0
    assert conn.execute("SELECT COUNT(*) FROM hard_memory").fetchone()[0] == 0
    assert [p.key for p in hard.pending()] == ["timezone"]


def test_approve_is_the_writing_path_and_versions(conn) -> None:
    hard = HardMemory(conn)
    first = hard.approve(hard.propose("timezone", "UTC"))
    second = hard.approve(hard.propose("timezone", "Asia/Seoul"))

    rows = conn.execute(
        "SELECT key, version, value, approved_by, active FROM hard_memory ORDER BY version"
    ).fetchall()
    assert first.version == 1 and second.version == 2
    assert [(row["value"], row["active"]) for row in rows] == [
        ("UTC", 0),
        ("Asia/Seoul", 1),
    ]
    assert all(row["approved_by"] == "user" for row in rows)
    assert hard.render().count("timezone") == 1
    assert "Asia/Seoul" in hard.render()


def test_reject_and_unknown_proposal(conn) -> None:
    hard = HardMemory(conn)
    proposal_id = hard.propose("disk", "SSD")
    hard.reject(proposal_id)

    with pytest.raises(ProposalError, match="no pending proposal"):
        hard.approve(proposal_id)
    assert conn.execute("SELECT COUNT(*) FROM hard_memory").fetchone()[0] == 0


def test_only_memory_hard_py_writes_hard_memory() -> None:
    pattern = re.compile(
        r"(INSERT INTO|UPDATE|DELETE FROM)\s+hard_memory", re.IGNORECASE
    )
    offenders = []
    for path in SRC.rglob("*.py"):
        if path.name == "hard.py" and path.parent.name == "memory":
            continue
        text = path.read_text(encoding="utf-8")
        if pattern.search(text):
            offenders.append(str(path.relative_to(SRC)))
    assert offenders == []


def test_service_approval_respects_the_cap(tmp_path: Path) -> None:
    db = tmp_path / "alice.db"
    init_db(db)
    conn = connect(db, create=False)
    try:
        core = tmp_path / "core.json"
        core.write_text('{"identity": {"name": "Alice"}}', encoding="utf-8")
        service = MemoryService(
            conn,
            MemoryConfig(core_memory_file=core, hard_max_tokens=30),
        )
        proposal_id = service.propose("long_fact", "x" * 400, "too much")

        with pytest.raises(MemoryCapError, match="over the 30-token cap"):
            service.approve(proposal_id)
        assert conn.execute("SELECT COUNT(*) FROM hard_memory").fetchone()[0] == 0
    finally:
        conn.close()


def test_service_hard_block_merges_core_and_approved(tmp_path: Path) -> None:
    db = tmp_path / "alice.db"
    init_db(db)
    conn = connect(db, create=False)
    try:
        core = tmp_path / "core.json"
        core.write_text('{"rules": ["never act"]}', encoding="utf-8")
        service = MemoryService(conn, MemoryConfig(core_memory_file=core))
        service.approve(service.propose("project", "Alice"))

        block = service.hard_block()

        assert "never act" in block
        assert "project: Alice" in block
    finally:
        conn.close()
