"""Archive recall: FTS5 keyword search plus optional sqlite-vec similarity."""

from __future__ import annotations

import logging
import re
import sqlite3
from array import array
from typing import Iterable

from ..contracts.memory import Embedder, MemoryItem, Recall
from .config import MemoryConfig
from .errors import ArchiveError
from ..core.tokens import estimate_tokens

logger = logging.getLogger(__name__)

_WORD = re.compile(r"[0-9A-Za-z가-힣_]+")


class Archive:
    def __init__(
        self,
        conn: sqlite3.Connection,
        config: MemoryConfig,
        *,
        embedder: Embedder | None = None,
    ) -> None:
        self.conn = conn
        self.config = config
        self.embedder = embedder
        self.model_name = config.embedding.model if config.embedding else "custom"
        self.vec_enabled = False
        if embedder is not None:
            self.vec_enabled = self._load_vec()

    def recall(
        self, query: str, k: int | None = None, since: str | None = None
    ) -> Recall:
        limit = k or self.config.recall_k
        candidates: dict[tuple[str, int], MemoryItem] = {}

        if query.strip():
            for item in self._fts(query, since, limit * 3):
                _keep(candidates, item)
            if self.vec_enabled:
                try:
                    for item in self._vector(query, since, limit * 3):
                        _keep(candidates, item)
                except Exception:
                    logger.exception("vector recall failed; using FTS results only")
        else:
            for item in self._recent(since, limit * 3):
                _keep(candidates, item)

        ranked = sorted(
            candidates.values(),
            key=lambda item: (0 if item.kind == "summary" else 1, -item.score),
        )

        items: list[MemoryItem] = []
        used = 0
        truncated = False
        for item in ranked:
            if len(items) >= limit:
                truncated = True
                break
            tokens = estimate_tokens(item.text)
            if used + tokens > self.config.recall_budget_tokens:
                truncated = True
                continue
            items.append(item)
            used += tokens
        return Recall(items=tuple(items), used_tokens=used, truncated=truncated)

    def index_pending(self, limit: int = 100) -> int:
        """Embed events and summaries that do not have a vector yet."""
        if self.embedder is None or not self.vec_enabled:
            return 0

        pending: list[tuple[str, int, str]] = []
        for source, table, text_column, order in (
            ("event", "events", "text_redacted", "ts"),
            ("summary", "summaries", "text", "period_start"),
        ):
            sql = f"""
                SELECT x.id, x.{text_column} AS text
                FROM {table} x
                LEFT JOIN memory_embeddings m
                  ON m.source = ? AND m.source_id = x.id
                WHERE m.source_id IS NULL
                ORDER BY x.{order} DESC
                LIMIT ?
            """
            rows = self.conn.execute(sql, (source, limit)).fetchall()
            pending.extend((source, int(row["id"]), str(row["text"])) for row in rows)

        if not pending:
            return 0
        vectors = self.embedder.embed([text for _, _, text in pending])
        for (source, source_id, _), vector in zip(pending, vectors):
            if len(vector) != self.config.embedding.dimension:
                raise ArchiveError(
                    f"embedder returned {len(vector)} dims; config says "
                    f"{self.config.embedding.dimension}"
                )
            self.conn.execute(
                """
                INSERT INTO memory_embeddings
                  (source, source_id, model, dim, vector, created)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(source, source_id) DO UPDATE SET
                  model = excluded.model,
                  dim = excluded.dim,
                  vector = excluded.vector,
                  created = excluded.created
                """,
                (
                    source,
                    source_id,
                    self.model_name,
                    len(vector),
                    _pack(vector),
                    _now(),
                ),
            )
        self.conn.commit()
        return len(pending)

    # -- internals ---------------------------------------------------------

    def _load_vec(self) -> bool:
        try:
            import sqlite_vec
        except ImportError:
            logger.warning("sqlite-vec is not installed; recall is FTS-only")
            return False
        try:
            self.conn.enable_load_extension(True)
            self.conn.load_extension(sqlite_vec.loadable_path())
            self.conn.enable_load_extension(False)
            return True
        except Exception:
            logger.exception("could not load sqlite-vec; recall is FTS-only")
            return False

    def _fts(self, query: str, since: str | None, limit: int) -> list[MemoryItem]:
        match = _match_query(query)
        if not match:
            return []
        items: list[MemoryItem] = []

        sql = """
            SELECT s.id, s.period_start AS ts, s.text, bm25(summaries_fts) AS rank
            FROM summaries_fts
            JOIN summaries s ON s.id = summaries_fts.rowid
            WHERE summaries_fts MATCH ?
        """
        params: list[object] = [match]
        if since:
            sql += " AND s.period_start >= ?"
            params.append(since)
        sql += " ORDER BY rank LIMIT ?"
        params.append(limit)
        for row in self.conn.execute(sql, params):
            items.append(
                MemoryItem(
                    kind="summary",
                    text=str(row["text"]),
                    score=-float(row["rank"]),
                    source_ids=(int(row["id"]),),
                )
            )

        sql = """
            SELECT e.id, e.ts, e.text_redacted AS text, bm25(events_fts) AS rank
            FROM events_fts
            JOIN events e ON e.id = events_fts.rowid
            WHERE events_fts MATCH ?
        """
        params = [match]
        if since:
            sql += " AND e.ts >= ?"
            params.append(since)
        sql += " ORDER BY rank LIMIT ?"
        params.append(limit)
        for row in self.conn.execute(sql, params):
            items.append(
                MemoryItem(
                    kind="event",
                    text=str(row["text"]),
                    score=-float(row["rank"]),
                    source_ids=(int(row["id"]),),
                )
            )
        return items

    def _vector(self, query: str, since: str | None, limit: int) -> list[MemoryItem]:
        assert self.embedder is not None
        vector = self.embedder.embed([query])[0]
        rows = self.conn.execute(
            """
            SELECT source, source_id, vec_distance_cosine(vector, ?) AS distance
            FROM memory_embeddings
            ORDER BY distance
            LIMIT ?
            """,
            (_pack(vector), limit),
        ).fetchall()
        items: list[MemoryItem] = []
        for row in rows:
            source = str(row["source"])
            source_id = int(row["source_id"])
            loaded = self._load_text(source, source_id)
            if loaded is None:
                continue
            ts, text = loaded
            if since and ts < since:
                continue
            items.append(
                MemoryItem(
                    kind="summary" if source == "summary" else "event",
                    text=text,
                    score=1.0 - float(row["distance"]),
                    source_ids=(source_id,),
                )
            )
        return items

    def _load_text(self, source: str, source_id: int) -> tuple[str, str] | None:
        if source == "summary":
            row = self.conn.execute(
                "SELECT period_start AS ts, text FROM summaries WHERE id = ?",
                (source_id,),
            ).fetchone()
        elif source == "event":
            row = self.conn.execute(
                "SELECT ts, text_redacted AS text FROM events WHERE id = ?",
                (source_id,),
            ).fetchone()
        else:
            return None
        if row is None:
            return None
        return str(row["ts"]), str(row["text"])

    def _recent(self, since: str | None, limit: int) -> Iterable[MemoryItem]:
        sql = "SELECT id, period_start AS ts, text FROM summaries"
        params: list[object] = []
        if since:
            sql += " WHERE period_start >= ?"
            params.append(since)
        sql += " ORDER BY period_start DESC LIMIT ?"
        params.append(limit)
        for row in self.conn.execute(sql, params):
            yield MemoryItem(
                kind="summary",
                text=str(row["text"]),
                score=1.0,
                source_ids=(int(row["id"]),),
            )

        sql = "SELECT id, ts, text_redacted AS text FROM events"
        params = []
        if since:
            sql += " WHERE ts >= ?"
            params.append(since)
        sql += " ORDER BY ts DESC LIMIT ?"
        params.append(limit)
        for row in self.conn.execute(sql, params):
            yield MemoryItem(
                kind="event",
                text=str(row["text"]),
                score=0.5,
                source_ids=(int(row["id"]),),
            )


def _keep(candidates: dict[tuple[str, int], MemoryItem], item: MemoryItem) -> None:
    key = (item.kind, item.source_ids[0] if item.source_ids else -1)
    existing = candidates.get(key)
    if existing is None or item.score > existing.score:
        candidates[key] = item


def _match_query(query: str) -> str:
    tokens = [token for token in _WORD.findall(query) if len(token) >= 2]
    return " OR ".join(f'"{token}"' for token in tokens)


def _pack(vector: list[float]) -> bytes:
    return array("f", vector).tobytes()


def _now() -> str:
    from ..core.db import utc_now

    return utc_now()
