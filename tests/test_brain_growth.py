from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from alice.brain.growth import GrowthConfig, GrowthService, parse_growth
from alice.contracts.models import Completion
from alice.core.config import ConfigError
from alice.core.db import connect, init_db


class StubRouter:
    def __init__(self, notes):
        self.notes = notes
        self.calls = 0

    def complete(self, tier, messages, max_tokens, *, purpose=None, cache_prefix_len=None):
        self.calls += 1
        return Completion(
            text=json.dumps({"notes": self.notes}),
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


def service(conn, router=None, **kwargs) -> GrowthService:
    config = GrowthConfig(
        max_notes_per_session=5,
        max_weight=0.8,
        daily_decay=0.995,
        influence_alpha=0.25,
        render_max_tokens=300,
        min_weight=0.03,
    )
    return GrowthService(conn, router, config, **kwargs)


def note(key="history-of-clocks", confidence=0.6):
    return {
        "key": key,
        "kind": "interest",
        "text": "A growing interest in the history of timekeeping.",
        "confidence": confidence,
    }


def test_growth_weight_moves_slowly_and_accumulates(conn) -> None:
    config = GrowthConfig(min_weight=0.0)
    router = StubRouter([note()])
    growth = GrowthService(conn, router, config)

    first = growth.ingest_session(summary="Read about clocks.")
    memories = growth.memories(min_weight=0.0)
    assert first > 0
    assert len(memories) == 1
    assert 0.0 < memories[0].weight < 0.2
    assert memories[0].evidence_count == 1

    growth.ingest_session(summary="Read about clocks again.")
    memories = growth.memories(min_weight=0.0)
    assert memories[0].evidence_count == 2
    assert 0.2 < memories[0].weight < 0.35
    assert router.calls == 2


def test_same_key_deduplicates_and_caps(conn) -> None:
    config = GrowthConfig(max_weight=0.4, min_weight=0.0, influence_alpha=0.5)
    router = StubRouter([note(confidence=0.9)])
    growth = GrowthService(conn, router, config)
    for _ in range(20):
        growth.ingest_session(summary="clocks")
    memories = growth.memories(min_weight=0.0)
    assert len(memories) == 1
    assert memories[0].weight <= 0.4
    assert memories[0].evidence_count == 20


def test_decay_reduces_effective_weight(conn) -> None:
    config = GrowthConfig(daily_decay=0.9, min_weight=0.0)
    now = datetime(2026, 11, 24, tzinfo=timezone.utc)
    growth = GrowthService(
        conn, StubRouter([note()]), config, now=lambda: now
    )
    growth.ingest_session(summary="clocks")
    before = growth.memories(min_weight=0.0)[0].weight

    later = GrowthService(
        conn,
        None,
        config,
        now=lambda: now + timedelta(days=10),
    )
    after = later.memories(min_weight=0.0)[0].weight
    assert after < before
    assert after == pytest.approx(before * 0.9**10, rel=1e-3)


def test_render_is_subordinate_and_weighted(conn) -> None:
    growth = service(conn, StubRouter([note()]))
    growth.ingest_session(summary="clocks")
    text = growth.render()
    assert "[GROWTH MEMORY]" in text
    assert "below core memory" in text
    assert "history-of-clocks" not in text.lower()
    assert "history of timekeeping" in text
    assert "(0." in text


def test_fallback_uses_titles_without_router(conn) -> None:
    growth = service(conn)
    growth.ingest_session(
        summary="A short session.",
        steps=[
            {"title": "Ice Warrior - Wikipedia"},
            {"title": "Coordinated Universal Time - Wikipedia"},
        ],
    )
    memories = growth.memories()
    assert len(memories) >= 2
    assert any("Ice Warrior" in memory.text for memory in memories)


def test_ingest_directory_reads_summary_and_steps(conn, tmp_path: Path) -> None:
    session = tmp_path / "exploration" / "session"
    session.mkdir(parents=True)
    (session / "summary.md").write_text("# Summary\n\nA session about maps.", encoding="utf-8")
    (session / "steps.jsonl").write_text(
        json.dumps(
            {
                "title": "Map - Wikipedia",
                "url": "https://en.wikipedia.org/wiki/Map",
                "input_tokens": 10,
                "output_tokens": 2,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (session.parent / "latest.json").write_text(
        json.dumps({"status": "completed", "started": "a", "ended": "b"}),
        encoding="utf-8",
    )
    growth = service(conn, StubRouter([note(key="maps")]))
    session_id = growth.ingest_directory(session)
    assert session_id > 0
    assert growth.sessions()[0].steps == 1
    assert growth.sessions()[0].input_tokens == 10
    assert growth.memories()[0].key == "maps"


def test_parse_growth_rejects_bad_values() -> None:
    assert parse_growth({}).max_weight == 0.8
    with pytest.raises(ConfigError):
        parse_growth({"daily_decay": 1.5})
    with pytest.raises(ConfigError):
        parse_growth({"unknown": True})
