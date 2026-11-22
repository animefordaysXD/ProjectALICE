"""Approved hard memory and proposals.

Only `approve()` writes `hard_memory`. Everything else may only insert into
`proposals`, so no model or sensor path can silently rewrite what Alice knows.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from ..core.db import utc_now
from .errors import ProposalError


@dataclass(frozen=True)
class ApprovedEntry:
    key: str
    value: str
    version: int
    created: str


@dataclass(frozen=True)
class Proposal:
    id: int
    key: str
    proposed_value: str
    reason: str | None
    created: str
    status: str


class HardMemory:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def active(self) -> list[ApprovedEntry]:
        rows = self.conn.execute(
            """
            SELECT key, value, version, created
            FROM hard_memory WHERE active = 1 ORDER BY key
            """
        ).fetchall()
        return [
            ApprovedEntry(
                key=row["key"],
                value=row["value"],
                version=int(row["version"]),
                created=row["created"],
            )
            for row in rows
        ]

    def render(self) -> str:
        entries = self.active()
        if not entries:
            return ""
        return render_entries({entry.key: entry.value for entry in entries})

    def propose(self, key: str, value: str, reason: str = "") -> int:
        key = key.strip()
        value = value.strip()
        if not key or not value:
            raise ProposalError("proposal key and value must not be empty")
        cursor = self.conn.execute(
            """
            INSERT INTO proposals (key, proposed_value, reason, created, status)
            VALUES (?, ?, ?, ?, 'pending')
            """,
            (key, value, reason.strip() or None, utc_now()),
        )
        self.conn.commit()
        return int(cursor.lastrowid)

    def pending(self) -> list[Proposal]:
        rows = self.conn.execute(
            "SELECT * FROM proposals WHERE status = 'pending' ORDER BY created"
        ).fetchall()
        return [_proposal(row) for row in rows]

    def get_pending(self, proposal_id: int) -> Proposal:
        row = self.conn.execute(
            "SELECT * FROM proposals WHERE id = ? AND status = 'pending'",
            (proposal_id,),
        ).fetchone()
        if row is None:
            raise ProposalError(f"no pending proposal with id {proposal_id}")
        return _proposal(row)

    def approve(self, proposal_id: int) -> ApprovedEntry:
        """The only path that writes hard_memory. Caller is the user's CLI."""
        proposal = self.get_pending(proposal_id)
        row = self.conn.execute(
            "SELECT COALESCE(MAX(version), 0) + 1 FROM hard_memory WHERE key = ?",
            (proposal.key,),
        ).fetchone()
        version = int(row[0])
        created = utc_now()
        try:
            self.conn.execute(
                "UPDATE hard_memory SET active = 0 WHERE key = ?", (proposal.key,)
            )
            self.conn.execute(
                """
                INSERT INTO hard_memory
                  (key, version, value, created, approved_by, active)
                VALUES (?, ?, ?, ?, 'user', 1)
                """,
                (proposal.key, version, proposal.proposed_value, created),
            )
            self.conn.execute(
                "UPDATE proposals SET status = 'approved' WHERE id = ?",
                (proposal_id,),
            )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return ApprovedEntry(
            key=proposal.key,
            value=proposal.proposed_value,
            version=version,
            created=created,
        )

    def reject(self, proposal_id: int) -> None:
        proposal = self.get_pending(proposal_id)
        self.conn.execute(
            "UPDATE proposals SET status = 'rejected' WHERE id = ?", (proposal.id,)
        )
        self.conn.commit()


def _proposal(row: sqlite3.Row) -> Proposal:
    return Proposal(
        id=int(row["id"]),
        key=str(row["key"]),
        proposed_value=str(row["proposed_value"]),
        reason=row["reason"],
        created=str(row["created"]),
        status=str(row["status"]),
    )


def render_entries(entries: dict[str, str]) -> str:
    if not entries:
        return ""
    lines = ["[APPROVED MEMORY]"]
    for key in sorted(entries):
        lines.append(f"{key}: {entries[key]}")
    return "\n".join(lines)
