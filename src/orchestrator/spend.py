"""Monthly model spend per tenant, and its cap (threat T14: denial of wallet).

Every metered request (chat, stream, review resolution, WhatsApp assistant) adds its known
cost to the tenant's month (UTC calendar month), with one atomic UPDATE, so several
replicas add up exactly. With `TENANT_MONTHLY_BUDGET_USD` above zero, a tenant at or over
its cap gets no more model calls until the month ends: the chat answers 429 with a clear
message, WhatsApp falls back to its fixed texts. Agenda, records, campaigns, crisis
alerts: unaffected. Unpriced calls (a self-hosted model, a remote agent) are counted
separately, never as free.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Column, DateTime, Float, Integer, String, Table, and_, insert, select, update
from sqlalchemy.exc import IntegrityError

from orchestrator.config import Settings
from orchestrator.db import Database, metadata, utcnow

model_spend = Table(
    "model_spend",
    metadata,
    Column("tenant", String(64), primary_key=True),
    Column("period", String(7), primary_key=True),  # YYYY-MM, UTC
    Column("cost_usd", Float, nullable=False),
    Column("llm_calls", Integer, nullable=False),
    Column("unpriced_calls", Integer, nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)


class BudgetExceeded(RuntimeError):
    """The tenant spent its monthly model budget."""


def period(now: datetime | None = None) -> str:
    return (now or utcnow()).strftime("%Y-%m")


class Spend:
    def __init__(self, db: Database, settings: Settings) -> None:
        self.db = db
        self.settings = settings

    async def month(self, tenant: str) -> dict[str, Any]:
        query = select(model_spend).where(
            and_(model_spend.c.tenant == tenant, model_spend.c.period == period())
        )
        async with self.db.engine.connect() as conn:
            row = (await conn.execute(query)).first()
        budget = self.settings.tenant_monthly_budget_usd
        cost = float(row.cost_usd) if row else 0.0
        return {
            "period": period(),
            "cost_usd": round(cost, 4),
            "llm_calls": row.llm_calls if row else 0,
            "unpriced_calls": row.unpriced_calls if row else 0,
            "budget_usd": budget or None,
            "used_ratio": round(cost / budget, 4) if budget else None,
        }

    async def check(self, tenant: str) -> None:
        budget = self.settings.tenant_monthly_budget_usd
        if budget and (await self.month(tenant))["cost_usd"] >= budget:
            raise BudgetExceeded(f"monthly model budget of {budget} USD reached")

    async def charge(self, tenant: str, usage: dict[str, Any]) -> None:
        cost = float(usage.get("cost_usd") or 0.0)
        calls = int(usage.get("llm_calls") or 0)
        unpriced = int(usage.get("unpriced_calls") or 0)
        if not (cost or calls or unpriced):
            return
        key = and_(model_spend.c.tenant == tenant, model_spend.c.period == period())
        now = utcnow()
        async with self.db.engine.begin() as conn:
            done = await conn.execute(
                update(model_spend)
                .where(key)
                .values(
                    cost_usd=model_spend.c.cost_usd + cost,
                    llm_calls=model_spend.c.llm_calls + calls,
                    unpriced_calls=model_spend.c.unpriced_calls + unpriced,
                    updated_at=now,
                )
            )
            if done.rowcount == 1:
                return
        try:
            async with self.db.engine.begin() as conn:
                await conn.execute(
                    insert(model_spend).values(
                        tenant=tenant,
                        period=period(),
                        cost_usd=cost,
                        llm_calls=calls,
                        unpriced_calls=unpriced,
                        updated_at=now,
                    )
                )
        except IntegrityError:  # another replica opened the month first: add to it
            await self.charge(tenant, usage)
