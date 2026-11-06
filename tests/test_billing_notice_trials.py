"""Private notice phases, opt-in, caps and stopping after a verified payment."""

import asyncio
from datetime import timedelta
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy import update

from orchestrator.billing import BillingError
from orchestrator.billing_notice_trials import NoticePolicy
from orchestrator.crm import patients
from orchestrator.db import utcnow
from tests.test_billing_followup import allow
from tests.test_billing_followup import world as world


async def test_all_phases_are_persisted_as_trials_with_three_daily_post_due_notices(
    world: Any,
) -> None:
    o, ids = world
    invoice = ids["acme-p1"]
    await allow(o)
    due = utcnow().replace(hour=15, minute=0, second=0, microsecond=0) + timedelta(days=1)
    await o.billing_ledger.deadline("acme", invoice, due, 1, "admin")
    service = o.billing_notice_trials
    for offset, phase in [
        (-1, "before_due"),
        (0, "due"),
        (1, "post_1"),
        (2, "post_2"),
        (3, "post_3"),
        (4, "escalation"),
    ]:
        result = await service.simulate("acme", invoice, "admin", due + timedelta(days=offset))
        assert result["phase"] == phase, result
        assert result["state"] == "simulated" and not result["sent"]
        assert result["provider_message_id"] is None
        again = await service.simulate("acme", invoice, "admin", due + timedelta(days=offset))
        assert again["state"] == "blocked", again
    await o.billing_ledger.credit(
        "acme",
        invoice,
        1000,
        "trial-bank-one",
        "trial-request-one",
        "Verified fictional bank",
        "admin",
    )
    partial = await service.simulate("acme", invoice, "admin", due + timedelta(days=4))
    assert partial["phase"] == "partial" and "39.99" in partial["text"]
    await o.billing_ledger.credit(
        "acme",
        invoice,
        3999,
        "trial-bank-two",
        "trial-request-two",
        "Verified fictional bank",
        "admin",
    )
    paid = await service.simulate("acme", invoice, "admin", due + timedelta(days=5))
    assert paid["phase"] == "paid" and "0.00" in paid["text"]
    assert (await service.simulate("acme", invoice, "admin", due + timedelta(days=6)))[
        "state"
    ] == "blocked"
    history = await service.history("acme", invoice)
    assert len(history) == 8 and sum(r["phase"].startswith("post_") for r in history) == 3
    assert all(r["state"] == "simulated" and r["provider_message_id"] is None for r in history)
    assert (await o.audit.verify("acme"))["ok"]
    exported = await o.billing.export_subject("acme", "p1")
    assert len(exported[0]["notice_simulations"]) == 8


async def test_permission_invalid_number_pauses_and_hours_are_respected(world: Any) -> None:
    o, ids = world
    invoice = ids["acme-p1"]
    due = utcnow().replace(hour=15, minute=0, second=0, microsecond=0)
    await o.billing_ledger.deadline("acme", invoice, due, 1, "admin")
    at = due + timedelta(days=1)
    service = o.billing_notice_trials
    assert (await service.simulate("acme", invoice, "admin", at))["reason"] == "permission_missing"
    await allow(o)
    assert (await service.simulate("acme", invoice, "admin", at.replace(hour=5)))[
        "reason"
    ] == "outside_hours"
    await o.payment_followups.schedule("acme", invoice, "admin", paused=True)
    assert (await service.simulate("acme", invoice, "admin", at))["reason"] == "paused"
    await o.payment_followups.schedule("acme", invoice, "admin", paused=False)
    async with o.db.engine.begin() as conn:
        await conn.execute(
            update(patients)
            .where(patients.c.tenant == "acme", patients.c.id == "p1")
            .values(phone="invalid")
        )
    assert (await service.simulate("acme", invoice, "admin", at))["reason"] == "phone_missing"
    assert await service.history("acme", invoice) == []
    with pytest.raises(KeyError):
        await service.history("globex", invoice)


async def test_simultaneous_trials_are_idempotent_and_not_a_production_sender(world: Any) -> None:
    o, ids = world
    invoice = ids["acme-p1"]
    await allow(o)
    due = utcnow().replace(hour=15, minute=0, second=0, microsecond=0)
    await o.billing_ledger.deadline("acme", invoice, due, 1, "admin")
    at = due + timedelta(days=1)
    results = await asyncio.gather(
        *[o.billing_notice_trials.simulate("acme", invoice, "admin", at) for _ in range(2)]
    )
    assert sorted(r["state"] for r in results) == ["blocked", "simulated"]
    assert len(await o.billing_notice_trials.history("acme", invoice)) == 1
    o.settings.app_env = "prod"
    with pytest.raises(BillingError, match="private test"):
        await o.billing_notice_trials.simulate("acme", invoice, "admin", at)


def test_templates_cannot_interpolate_arbitrary_properties_or_exceed_the_cap() -> None:
    policy = NoticePolicy().model_dump()
    policy["texts"]["due"] = "{balance.__class__}"
    with pytest.raises(ValidationError):
        NoticePolicy.model_validate(policy)
    with pytest.raises(ValidationError):
        NoticePolicy(maximum=4)
