"""SQL adapter of the spending ledger; tenant and month scope every operation."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Column, DateTime, Float, Integer, String, Table, and_, insert, select, update
from sqlalchemy.exc import IntegrityError

from orchestrator.budget import MonthlyUsage
from orchestrator.db import Database, metadata

model_spend = Table(
    "model_spend",
    metadata,
    Column("tenant", String(64), primary_key=True),
    Column("period", String(7), primary_key=True),
    Column("cost_usd", Float, nullable=False),
    Column("llm_calls", Integer, nullable=False),
    Column("unpriced_calls", Integer, nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)


class SqlSpendLedger:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def read_month(self, tenant: str, month: str) -> MonthlyUsage:
        query = select(model_spend).where(
            and_(model_spend.c.tenant == tenant, model_spend.c.period == month)
        )
        async with self.db.engine.connect() as conn:
            row = (await conn.execute(query)).first()
        return (
            MonthlyUsage(float(row.cost_usd), row.llm_calls, row.unpriced_calls)
            if row
            else MonthlyUsage()
        )

    async def _increment(self, tenant: str, month: str, usage: MonthlyUsage, at: datetime) -> bool:
        async with self.db.engine.begin() as conn:
            changed = await conn.execute(
                update(model_spend)
                .where(and_(model_spend.c.tenant == tenant, model_spend.c.period == month))
                .values(
                    cost_usd=model_spend.c.cost_usd + usage.cost_usd,
                    llm_calls=model_spend.c.llm_calls + usage.llm_calls,
                    unpriced_calls=model_spend.c.unpriced_calls + usage.unpriced_calls,
                    updated_at=at,
                )
            )
            return changed.rowcount == 1

    async def add(self, tenant: str, month: str, usage: MonthlyUsage, at: datetime) -> None:
        if await self._increment(tenant, month, usage, at):
            return
        try:
            async with self.db.engine.begin() as conn:
                await conn.execute(
                    insert(model_spend).values(
                        tenant=tenant,
                        period=month,
                        cost_usd=usage.cost_usd,
                        llm_calls=usage.llm_calls,
                        unpriced_calls=usage.unpriced_calls,
                        updated_at=at,
                    )
                )
        except IntegrityError:
            # Another replica opened this month. Keep the same accounting period and
            # timestamp; do not recurse and sample the clock again across midnight.
            if not await self._increment(tenant, month, usage, at):
                raise
