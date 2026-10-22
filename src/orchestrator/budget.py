"""Monthly spending policy and ledger port, independent of database and provider SDKs.

The limit is checked against unrounded accounting data; rounding belongs to display.
This is post-consumption accounting, not a reservation for future provider charges.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol


class BudgetExceeded(RuntimeError):
    """The tenant spent its monthly model budget."""


def utcnow() -> datetime:
    return datetime.now(UTC)


def period(now: datetime | None = None) -> str:
    moment = now or utcnow()
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).strftime("%Y-%m")


@dataclass(frozen=True)
class MonthlyUsage:
    cost_usd: float = 0.0
    llm_calls: int = 0
    unpriced_calls: int = 0

    def __post_init__(self) -> None:
        if not math.isfinite(self.cost_usd) or self.cost_usd < 0:
            raise ValueError("model cost must be finite and nonnegative")
        if self.llm_calls < 0 or self.unpriced_calls < 0:
            raise ValueError("usage call counts must be nonnegative")


class SpendLedger(Protocol):
    async def read_month(self, tenant: str, month: str) -> MonthlyUsage: ...

    async def add(self, tenant: str, month: str, usage: MonthlyUsage, at: datetime) -> None: ...


class Spend:
    def __init__(
        self,
        ledger: SpendLedger,
        monthly_budget_usd: float,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        if not math.isfinite(monthly_budget_usd) or monthly_budget_usd < 0:
            raise ValueError("monthly budget must be finite and nonnegative")
        self.ledger = ledger
        self._budget_usd = monthly_budget_usd
        self.clock = clock

    @property
    def budget_usd(self) -> float:
        return self._budget_usd

    async def month(self, tenant: str) -> dict[str, Any]:
        month = period(self.clock())
        usage = await self.ledger.read_month(tenant, month)
        budget = self.budget_usd
        return {
            "period": month,
            "cost_usd": round(usage.cost_usd, 4),
            "llm_calls": usage.llm_calls,
            "unpriced_calls": usage.unpriced_calls,
            "budget_usd": budget or None,
            "used_ratio": round(usage.cost_usd / budget, 4) if budget else None,
        }

    async def check(self, tenant: str) -> None:
        budget = self.budget_usd
        if not budget:
            return
        usage = await self.ledger.read_month(tenant, period(self.clock()))
        # SQL's existing FLOAT ledger can accumulate tiny floating-point error.
        if usage.cost_usd >= budget or math.isclose(usage.cost_usd, budget, rel_tol=1e-12):
            raise BudgetExceeded(f"monthly model budget of {budget} USD reached")

    async def charge(self, tenant: str, usage: dict[str, Any]) -> None:
        amount = MonthlyUsage(
            cost_usd=float(usage.get("cost_usd") or 0.0),
            llm_calls=int(usage.get("llm_calls") or 0),
            unpriced_calls=int(usage.get("unpriced_calls") or 0),
        )
        if not (amount.cost_usd or amount.llm_calls or amount.unpriced_calls):
            return
        now = self.clock()
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
        await self.ledger.add(tenant, period(now), amount, now)
