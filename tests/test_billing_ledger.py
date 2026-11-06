"""Confirmed partial payments, exact balances, duplicate bank references and isolation."""

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from orchestrator.billing import BillingError, ChargeKind
from orchestrator.billing_ledger import BillingLedger
from orchestrator.billing_store import BillingStore
from orchestrator.db import utcnow
from tests.test_billing_followup import allow
from tests.test_billing_followup import world as world
from tests.test_billing_store import billing as billing


async def test_partial_exact_balance_deadline_idempotence_and_full_settlement(
    billing: BillingStore,
) -> None:
    ledger = BillingLedger(billing)
    row = await billing.create("acme", ChargeKind.APPOINTMENT, "visit-a", "admin")
    invoice = row["invoice_id"]
    current = await ledger.statement("acme", invoice)
    assert current["balance_cents"] == 4999 and current["movements"] == []
    current = await ledger.deadline("acme", invoice, utcnow() - timedelta(days=1), 1, "admin")
    assert current["balance_state"] == "overdue"
    current = await ledger.credit(
        "acme", invoice, 1234, "bank-one", "request-one", "Verified in bank", "admin"
    )
    assert current["paid_cents"] == 1234 and current["balance_cents"] == 3765
    assert current["balance_state"] == "partial_overdue"
    assert len(current["movements"]) == 1 and current["state"] == "pending"
    again = await ledger.credit(
        "acme", invoice, 1234, "bank-one", "request-one", "Verified in bank", "admin"
    )
    assert again["revision"] == current["revision"] and len(again["movements"]) == 1
    with pytest.raises(BillingError):
        await ledger.credit(
            "acme", invoice, 1235, "bank-one", "request-one", "Verified in bank", "admin"
        )
    final = await ledger.credit(
        "acme", invoice, 3765, "bank-two", "request-two", "Verified remaining amount", "admin"
    )
    assert final["state"] == "paid" and final["balance_cents"] == 0
    assert sum(m["amount_cents"] for m in final["movements"]) == 4999
    with pytest.raises(BillingError):
        await ledger.credit(
            "acme", invoice, 1, "bank-three", "request-three", "Verified in bank", "admin"
        )
    exported = await billing.export_subject("acme", "patient")
    assert sum(len(i["confirmed_payments"]) for i in exported) == 2
    assert "bank-one" not in str(exported)  # internal settlement ids are excluded


async def test_simultaneous_abonos_cannot_overpay_and_reference_cannot_settle_another_order(
    billing: BillingStore,
) -> None:
    ledger = BillingLedger(billing)
    row = await billing.create("acme", ChargeKind.APPOINTMENT, "visit-a", "admin")
    invoice = row["invoice_id"]
    results = await asyncio.gather(
        *[
            ledger.credit(
                "acme", invoice, 3000, f"bank-{i}", f"request-{i}", "Verified in bank", "admin"
            )
            for i in range(2)
        ],
        return_exceptions=True,
    )
    assert sum(isinstance(r, BillingError) for r in results) == 1
    current = await ledger.statement("acme", invoice)
    assert current["paid_cents"] == 3000 and len(current["movements"]) == 1
    another = await billing.create("acme", ChargeKind.APPOINTMENT, "visit-b", "admin")
    bank = current["movements"][0]["bank_transaction_id"]
    await billing.notify_transfer("acme", another["invoice_id"], "reported-transfer", "patient")
    with pytest.raises(BillingError):
        await billing.review_transfer(
            "acme",
            another["invoice_id"],
            "admin",
            True,
            "Verified in bank",
            bank_transaction_id=bank,
        )
    with pytest.raises(KeyError):
        await ledger.statement("globex", invoice)


async def test_reminder_uses_remaining_balance_and_stops_when_paid(world: Any) -> None:
    o, ids = world
    await allow(o)
    invoice = ids["acme-p1"]
    old = await o.payment_followups.prepare("acme", invoice, "admin")
    await o.billing_ledger.credit(
        "acme", invoice, 1000, "bank-partial", "request-partial", "Verified in bank", "admin"
    )
    with pytest.raises(BillingError):
        await o.payment_followups.complete("acme", invoice, old["event_id"], "admin", True)
    draft = await o.payment_followups.prepare("acme", invoice, "admin")
    assert "39.99" in draft["message"] and "49.99" not in draft["message"]
    o.settings.public_base_url = "https://payments.example.test"
    with pytest.raises(BillingError, match="abonos"):
        await o.dlocal_billing.checkout("acme", invoice, "admin")
    await o.billing_ledger.credit(
        "acme", invoice, 3999, "bank-final", "request-final", "Verified in bank", "admin"
    )
    with pytest.raises(BillingError, match="paid"):
        await o.payment_followups.prepare("acme", invoice, "admin")
    assert (await o.billing_ledger.statement("acme", invoice))["balance_state"] == "paid"


async def test_app_subscriptions_have_their_own_recipient_and_scope(billing: BillingStore) -> None:
    ledger = BillingLedger(billing)
    row = await billing.create("acme", ChargeKind.SUBSCRIPTION, None, "admin")
    with pytest.raises(KeyError):
        await ledger.credit(
            "acme",
            row["invoice_id"],
            1000,
            "bank-app-one",
            "request-app-one",
            "Verified in platform bank",
            "operator",
        )
    record = await ledger.credit(
        "acme",
        row["invoice_id"],
        1000,
        "bank-app-one",
        "request-app-one",
        "Verified in platform bank",
        "operator",
        platform=True,
    )
    assert record["balance_cents"] == 1500 and record["state"] == "pending"


async def test_deadline_keeps_the_instant_across_timezones_and_rejects_old_versions(
    billing: BillingStore,
) -> None:
    ledger = BillingLedger(billing)
    row = await billing.create("acme", ChargeKind.APPOINTMENT, "visit-a", "admin")
    due = datetime.fromisoformat("2026-11-05T09:00:00-05:00")
    updated = await ledger.deadline("acme", row["invoice_id"], due, 1, "admin")
    assert updated["due_at"] == due.astimezone(UTC)
    with pytest.raises(BillingError, match="changed"):
        await ledger.deadline("acme", row["invoice_id"], due, 1, "admin")
