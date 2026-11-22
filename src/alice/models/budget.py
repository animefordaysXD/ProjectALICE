"""Cost caps, the kill switch, and usage accounting.

Every model call writes a row to `usage`. The guard refuses calls at the hard
daily cap (before they happen, using a deliberately pessimistic estimate) and
warns once a day at the soft cap. `alice pause` writes a small file that
blocks all model calls until `alice resume`.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .config import Budget
from ..core.db import utc_now

logger = logging.getLogger(__name__)

PAUSE_FILENAME = "paused.json"


class BudgetError(RuntimeError):
    """A call was refused because of the kill switch or a budget cap."""


@dataclass(frozen=True)
class PauseState:
    since: str
    reason: str


@dataclass(frozen=True)
class BudgetStatus:
    paused: PauseState | None
    calls_today: int
    spend_today: float
    soft_cap: float
    hard_cap: float

    @property
    def remaining(self) -> float:
        return max(0.0, self.hard_cap - self.spend_today)

    @property
    def soft_reached(self) -> bool:
        return self.spend_today >= self.soft_cap


def day_start(now: datetime | None = None) -> str:
    """Start of the current UTC day (caps reset at 00:00 UTC)."""
    moment = now or datetime.now(timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT00:00:00Z")


def pause_file(data_dir: Path | str) -> Path:
    return Path(data_dir) / PAUSE_FILENAME


def read_pause(data_dir: Path | str) -> PauseState | None:
    path = pause_file(data_dir)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.warning("unreadable pause file: %s", path)
        return PauseState(since="unknown", reason="unreadable pause file")
    return PauseState(since=str(data.get("since", "unknown")), reason=str(data.get("reason", "user")))


def set_pause(data_dir: Path | str, reason: str = "user") -> PauseState:
    path = pause_file(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    state = PauseState(since=utc_now(), reason=(reason or "").strip() or "user")
    temp = path.with_name(f"{path.name}.tmp")
    temp.write_text(
        json.dumps({"since": state.since, "reason": state.reason}), encoding="utf-8"
    )
    temp.replace(path)
    logger.info("model calls paused: %s", state.reason)
    return state


def clear_pause(data_dir: Path | str) -> bool:
    path = pause_file(data_dir)
    if not path.is_file():
        return False
    path.unlink()
    logger.info("model calls resumed")
    return True


class UsageLog:
    """Token and cost accounting, one row per call."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def record(
        self,
        *,
        tier: str,
        model: str,
        input_tokens: int,
        cached_tokens: int,
        output_tokens: int,
        cost_usd: float,
        purpose: str | None = None,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO usage
              (ts, tier, model, input_tokens, cached_tokens, output_tokens,
               cost_usd, purpose)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                utc_now(),
                tier,
                model,
                int(input_tokens),
                int(cached_tokens),
                int(output_tokens),
                float(cost_usd),
                purpose,
            ),
        )
        self.conn.commit()
        logger.info(
            "model call tier=%s model=%s input=%d cached=%d output=%d cost=$%.6f purpose=%s",
            tier,
            model,
            input_tokens,
            cached_tokens,
            output_tokens,
            cost_usd,
            purpose or "-",
        )

    def totals_since(self, since: str) -> tuple[int, float]:
        row = self.conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(cost_usd), 0) FROM usage WHERE ts >= ?",
            (since,),
        ).fetchone()
        return int(row[0]), float(row[1])

    def spend_since(self, since: str) -> float:
        return self.totals_since(since)[1]


class BudgetGuard:
    def __init__(
        self, conn: sqlite3.Connection, budget: Budget, data_dir: Path | str
    ) -> None:
        self.usage = UsageLog(conn)
        self.budget = budget
        self.data_dir = Path(data_dir)
        self._warned_on: str | None = None

    def pause(self, reason: str = "user") -> PauseState:
        return set_pause(self.data_dir, reason)

    def resume(self) -> bool:
        return clear_pause(self.data_dir)

    def paused(self) -> PauseState | None:
        return read_pause(self.data_dir)

    def status(self) -> BudgetStatus:
        calls, spend = self.usage.totals_since(day_start())
        return BudgetStatus(
            paused=self.paused(),
            calls_today=calls,
            spend_today=spend,
            soft_cap=self.budget.soft_daily_usd,
            hard_cap=self.budget.hard_daily_usd,
        )

    def check(self, *, tier: str, estimated_usd: float) -> None:
        """Refuse a call that would violate the kill switch or hard cap."""
        paused = self.paused()
        if paused is not None:
            raise BudgetError(
                f"model calls are paused since {paused.since} ({paused.reason}); "
                "run `alice resume` to allow them"
            )

        spend = self.usage.spend_since(day_start())
        hard = self.budget.hard_daily_usd
        if spend >= hard:
            raise BudgetError(
                f"hard daily cap reached for tier '{tier}': ${spend:.4f} of ${hard:.2f}; "
                "calls are blocked until 00:00 UTC"
            )
        if spend + estimated_usd > hard:
            raise BudgetError(
                f"tier '{tier}' call refused: ${spend:.4f} spent + "
                f"${estimated_usd:.4f} estimated would exceed the ${hard:.2f} hard cap"
            )
        self._warn_soft(spend)

    def note_spend(self, amount: float) -> None:
        self._warn_soft(self.usage.spend_since(day_start()))

    def _warn_soft(self, spend: float) -> None:
        if spend < self.budget.soft_daily_usd:
            return
        today = day_start()
        if self._warned_on == today:
            return
        self._warned_on = today
        logger.warning(
            "daily soft cap reached: $%.4f of $%.2f (hard cap $%.2f)",
            spend,
            self.budget.soft_daily_usd,
            self.budget.hard_daily_usd,
        )
