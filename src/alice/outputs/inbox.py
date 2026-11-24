"""In-app notification inbox shared by the runtime and the GUI."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from ..core.db import utc_now


@dataclass(frozen=True)
class NotificationRow:
    id: int
    created: str
    kind: str
    title: str
    body: str
    suggestion_id: int | None
    read: bool
    read_at: str | None


class NotificationCenter:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def add(
        self,
        *,
        kind: str,
        title: str,
        body: str,
        suggestion_id: int | None = None,
    ) -> int:
        cursor = self.conn.execute(
            "INSERT INTO notifications "
            "(created, kind, title, body, suggestion_id, read) "
            "VALUES (?, ?, ?, ?, ?, 0)",
            (utc_now(), kind, title[:80], body[:600], suggestion_id),
        )
        return int(cursor.lastrowid)

    def unread(self, limit: int = 20) -> tuple[NotificationRow, ...]:
        return self._rows(
            "SELECT id, created, kind, title, body, suggestion_id, read, read_at "
            "FROM notifications WHERE read = 0 ORDER BY id DESC LIMIT ?",
            (limit,),
        )

    def recent(self, limit: int = 50) -> tuple[NotificationRow, ...]:
        return self._rows(
            "SELECT id, created, kind, title, body, suggestion_id, read, read_at "
            "FROM notifications ORDER BY id DESC LIMIT ?",
            (limit,),
        )

    def unread_count(self) -> int:
        return int(
            self.conn.execute(
                "SELECT COUNT(*) FROM notifications WHERE read = 0"
            ).fetchone()[0]
        )

    def mark_read(self, notification_id: int) -> None:
        self.conn.execute(
            "UPDATE notifications SET read = 1, read_at = ? "
            "WHERE id = ? AND read = 0",
            (utc_now(), notification_id),
        )
        self.conn.commit()

    def mark_all_read(self) -> int:
        cursor = self.conn.execute(
            "UPDATE notifications SET read = 1, read_at = ? WHERE read = 0",
            (utc_now(),),
        )
        self.conn.commit()
        return int(cursor.rowcount or 0)

    def _rows(self, sql: str, params: tuple[object, ...]) -> tuple[NotificationRow, ...]:
        return tuple(
            NotificationRow(
                id=int(row["id"]),
                created=str(row["created"]),
                kind=str(row["kind"]),
                title=str(row["title"]),
                body=str(row["body"]),
                suggestion_id=row["suggestion_id"],
                read=bool(row["read"]),
                read_at=row["read_at"],
            )
            for row in self.conn.execute(sql, params)
        )
