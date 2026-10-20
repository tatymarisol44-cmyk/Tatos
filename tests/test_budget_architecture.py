from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from orchestrator.service import Orchestrator
from orchestrator.spend import period


async def test_display_rounding_must_not_exhaust_remaining_budget(
    orchestrator: Orchestrator,
) -> None:
    orchestrator.settings.tenant_monthly_budget_usd = 1.0
    await orchestrator.spend.charge("acme", {"cost_usd": 0.99996, "llm_calls": 1})
    assert (await orchestrator.spend.month("acme"))["cost_usd"] == 1.0
    await orchestrator.spend.check("acme")


@pytest.mark.parametrize("cost", [-0.01, float("nan"), float("inf"), float("-inf")])
async def test_invalid_cost_cannot_change_the_ledger(
    orchestrator: Orchestrator, cost: float
) -> None:
    with pytest.raises(ValueError, match="cost"):
        await orchestrator.spend.charge("acme", {"cost_usd": cost, "llm_calls": 1})
    assert (await orchestrator.spend.month("acme"))["cost_usd"] == 0.0


def test_monthly_period_is_always_utc() -> None:
    local = datetime(2026, 10, 31, 20, 30, tzinfo=timezone(timedelta(hours=-5)))
    assert period(local) == "2026-11"


async def test_policy_can_run_without_a_database_and_samples_the_period_once() -> None:
    from datetime import UTC

    from orchestrator.budget import MonthlyUsage, Spend

    instants = iter([datetime(2026, 10, 31, 23, 59, tzinfo=UTC), datetime(2026, 11, 1, tzinfo=UTC)])
    seen = []

    class Ledger:
        async def read_month(self, tenant: str, month: str) -> MonthlyUsage:
            seen.append((tenant, month))
            return MonthlyUsage(0.25, 2, 1)

        async def add(self, tenant: str, month: str, usage: MonthlyUsage, at: datetime) -> None:
            seen.append((tenant, month))

    policy = Spend(Ledger(), 1.0, clock=lambda: next(instants))
    month = await policy.month("acme")
    assert month["period"] == "2026-10" and seen == [("acme", "2026-10")]
    await policy.charge("globex", {"cost_usd": 0.1, "llm_calls": 1})
    assert seen[-1] == ("globex", "2026-11")


async def test_float_accumulation_cannot_bypass_a_reached_cap(orchestrator: Orchestrator) -> None:
    from orchestrator.spend import BudgetExceeded

    orchestrator.settings.tenant_monthly_budget_usd = 1.0
    for _ in range(10):
        await orchestrator.spend.charge("acme", {"cost_usd": 0.1, "llm_calls": 1})
    with pytest.raises(BudgetExceeded):
        await orchestrator.spend.check("acme")


@pytest.mark.parametrize("field", ["llm_calls", "unpriced_calls"])
async def test_negative_call_counts_are_rejected(orchestrator: Orchestrator, field: str) -> None:
    with pytest.raises(ValueError, match="counts"):
        await orchestrator.spend.charge("acme", {field: -1})
