"""Event data model, ledger writes, cursors, and raw payload storage.

Raw text is held locally only. The ledger stores redacted text plus an
optional pointer into the content-addressed raw store.
"""

from __future__ import annotations

import hashlib
import logging
import os
import sqlite3
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Mapping

from .db import utc_now

logger = logging.getLogger(__name__)

DEFAULT_RAW_MAX_BYTES = 64 * 1024


@dataclass(frozen=True)
class RawEvent:
    """Something a sensor observed, before redaction and gating."""

    source: str
    kind: str
    text: str
    ts: str
    meta: Mapping[str, Any] = field(default_factory=dict)

    @property
    def source_type(self) -> str:
        return self.source.split(":", 1)[0]


@dataclass(frozen=True)
class Event:
    """A row of the append-only event ledger."""

    id: int
    ts: str
    source: str
    kind: str
    content_hash: str
    text_redacted: str
    raw_ref: str | None
    salience: float
    status: str
    count: int
    last_seen: str | None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Event":
        return cls(
            id=int(row["id"]),
            ts=str(row["ts"]),
            source=str(row["source"]),
            kind=str(row["kind"]),
            content_hash=str(row["content_hash"]),
            text_redacted=str(row["text_redacted"]),
            raw_ref=row["raw_ref"],
            salience=float(row["salience"] or 0.0),
            status=str(row["status"]),
            count=int(row["count"]),
            last_seen=row["last_seen"],
        )


class WriteKind(str, Enum):
    INSERTED = "inserted"
    COLLAPSED = "collapsed"
    DUPLICATE = "duplicate"


@dataclass(frozen=True)
class LedgerWrite:
    kind: WriteKind
    event_id: int
    count: int


class Ledger:
    """Append-only event writes with dedupe and repetition collapse."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def last_seen(self, content_hash: str) -> str | None:
        row = self.conn.execute(
            "SELECT last_seen FROM events WHERE content_hash = ?", (content_hash,)
        ).fetchone()
        return None if row is None else row["last_seen"]

    def get(self, event_id: int) -> Event | None:
        row = self.conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
        return None if row is None else Event.from_row(row)

    def pending(self, min_salience: float, limit: int = 1000) -> list[Event]:
        rows = self.conn.execute(
            """
            SELECT * FROM events
            WHERE status = 'new' AND salience >= ?
            ORDER BY ts, id
            LIMIT ?
            """,
            (min_salience, limit),
        ).fetchall()
        return [Event.from_row(row) for row in rows]

    def mark_status(self, event_ids: list[int], status: str) -> None:
        if not event_ids:
            return
        placeholders = ",".join("?" for _ in event_ids)
        self.conn.execute(
            f"UPDATE events SET status = ? WHERE id IN ({placeholders})",
            [status, *event_ids],
        )
        self.conn.commit()

    def record(
        self,
        *,
        ts: str,
        source: str,
        kind: str,
        content_hash: str,
        text_redacted: str,
        raw_ref: str | None,
        salience: float,
        last_seen: str,
    ) -> LedgerWrite:
        """Insert, collapse into, or drop an event.

        Exact repeats are dropped (no new row). Near-identical repeats raise
        the existing row's count. Every repeat refreshes last_seen so novelty
        checks do not treat a noisy line as new.
        """
        existing = self.conn.execute(
            "SELECT id, text_redacted, count FROM events WHERE content_hash = ?",
            (content_hash,),
        ).fetchone()

        if existing is None:
            cursor = self.conn.execute(
                """
                INSERT INTO events
                  (ts, source, kind, content_hash, text_redacted, raw_ref,
                   salience, status, count, last_seen)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'new', 1, ?)
                """,
                (ts, source, kind, content_hash, text_redacted, raw_ref, salience, last_seen),
            )
            self.conn.commit()
            return LedgerWrite(WriteKind.INSERTED, int(cursor.lastrowid), 1)

        event_id = int(existing["id"])
        if existing["text_redacted"] == text_redacted:
            self.conn.execute(
                "UPDATE events SET last_seen = ?, salience = MAX(salience, ?) WHERE id = ?",
                (last_seen, salience, event_id),
            )
            self.conn.commit()
            return LedgerWrite(WriteKind.DUPLICATE, event_id, int(existing["count"]))

        self.conn.execute(
            """
            UPDATE events
            SET count = count + 1, last_seen = ?, salience = MAX(salience, ?)
            WHERE id = ?
            """,
            (last_seen, salience, event_id),
        )
        self.conn.commit()
        return LedgerWrite(WriteKind.COLLAPSED, event_id, int(existing["count"]) + 1)


class CursorStore:
    """Sensor progress, keyed by source, persisted across restarts."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def get(self, source: str) -> str | None:
        row = self.conn.execute(
            "SELECT position FROM cursors WHERE source = ?", (source,)
        ).fetchone()
        return None if row is None else str(row["position"])

    def set(self, source: str, position: str) -> None:
        self.conn.execute(
            """
            INSERT INTO cursors (source, position, updated) VALUES (?, ?, ?)
            ON CONFLICT(source) DO UPDATE
              SET position = excluded.position, updated = excluded.updated
            """,
            (source, position, utc_now()),
        )
        self.conn.commit()

    def count(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) FROM cursors").fetchone()[0])


class RawStore:
    """Content-addressed raw payloads for locally redacted text.

    Only payloads that redaction changed are worth keeping: otherwise the
    redacted ledger text is already the raw text. Payloads over max_bytes are
    skipped and the event keeps raw_ref NULL.
    """

    def __init__(self, root: Path, *, max_bytes: int = DEFAULT_RAW_MAX_BYTES) -> None:
        self.root = root
        self.max_bytes = max_bytes

    def save(self, text: str) -> str | None:
        payload = text.encode("utf-8")
        if len(payload) > self.max_bytes:
            logger.warning(
                "raw payload skipped: %d bytes exceeds limit %d", len(payload), self.max_bytes
            )
            return None

        digest = hashlib.sha256(payload).hexdigest()
        relative = Path(digest[:2]) / f"{digest}.txt"
        path = self.root / relative
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            temp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
            temp.write_bytes(payload)
            os.replace(temp, path)
        return relative.as_posix()
