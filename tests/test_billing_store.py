"""Persistent transfer workflow with synthetic bank references and no external calls."""

from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import insert

from orchestrator.billing import BillingError, ChargeKind
from orchestrator.billing_store import BillingStore, orders
from orchestrator.config import Settings
from orchestrator.crm import appointments, patients
from orchestrator.db import utcnow
from orchestrator.service import Orchestrator


@pytest.fixture
async def billing(orchestrator: Orchestrator, settings: Settings) -> BillingStore:
    settings.billing_enabled = True
    settings.billing_platform_tenant = "platform-test"
    settings.billing_monthly_price_cents = 2500  # synthetic test price, not a business decision
    store = BillingStore(orchestrator.db, orchestrator.audit, settings)
    for tenant in ["acme", "globex"]:
        await store.configure_bank(tenant, "clinic", "Synthetic clinic bank instructions", "admin")
    await store.configure_bank(
        "platform-test",
        "platform",
        "Synthetic PLATFORM bank instructions",
        "operator",
        operator=True,
    )
    now = utcnow()
    async with orchestrator.db.engine.begin() as conn:
        for tenant in ["acme", "globex"]:
            await conn.execute(
                insert(patients).values(
                    tenant=tenant,
                    id="patient",
                    display_name="Paciente ficticia",
                    restricted=False,
                    created_at=now,
                )
            )
            for visit in ["visit-a", "visit-b"]:
                await conn.execute(
                    insert(appointments).values(
                        tenant=tenant,
                        id=visit,
                        patient_id="patient",
                        starts_at=now + timedelta(days=1),
                        duration_min=50,
                        kind="follow_up",
                        status="scheduled",
                        price=Decimal("49.99"),
                        created_at=now,
                        updated_at=now,
                    )
                )
    return store


async def test_create_reads_the_appointment_price_and_keeps_a_destination_snapshot(
    billing: BillingStore,
) -> None:
    order = await billing.create("acme", ChargeKind.APPOINTMENT, "visit-a", "reception")
    assert order["amount_cents"] == 4999 and order["state"] == "pending"
    assert order["bank_instructions"] == "Synthetic clinic bank instructions"
    await billing.configure_bank("acme", "clinic", "Changed future bank instructions", "admin")
    retry = await billing.create("acme", ChargeKind.APPOINTMENT, "visit-a", "reception")
    assert retry["invoice_id"] == order["invoice_id"]
    assert retry["bank_instructions"] == order["bank_instructions"]
    sub = await billing.create("acme", ChargeKind.SUBSCRIPTION, None, "admin")
    assert sub["amount_cents"] == 2500
    assert sub["bank_instructions"] == "Synthetic PLATFORM bank instructions"
    assert (await billing.create("acme", ChargeKind.SUBSCRIPTION, None, "admin"))[
        "invoice_id"
    ] == sub["invoice_id"]


async def test_patients_and_tenants_cannot_report_somebody_elses_payment(
    billing: BillingStore,
) -> None:
    order = await billing.create("acme", ChargeKind.APPOINTMENT, "visit-a", "reception")
    for tenant, subject in [("globex", "patient"), ("acme", "another-patient")]:
        with pytest.raises(KeyError):
            await billing.notify_transfer(
                tenant, order["invoice_id"], "demo-reference", "payer", patient_id=subject
            )
    with pytest.raises(KeyError):
        await billing.create(
            "acme", ChargeKind.APPOINTMENT, "visit-a", "payer", patient_id="another-patient"
        )
    with pytest.raises(BillingError):
        await billing.create("acme", ChargeKind.SUBSCRIPTION, None, "payer", patient_id="patient")
    assert await billing.listing("acme", ChargeKind.APPOINTMENT, patient_id="another-patient") == []


async def test_only_the_recipient_can_verify_a_transfer(billing: BillingStore) -> None:
    order = await billing.create("acme", ChargeKind.SUBSCRIPTION, None, "admin")
    pending = await billing.notify_transfer("acme", order["invoice_id"], "demo-reference", "admin")
    assert pending["state"] == "transfer_review"
    with pytest.raises(BillingError):
        await billing.review_transfer(
            "acme",
            order["invoice_id"],
            "admin",
            True,
            "Verified in bank",
            bank_transaction_id="bank-demo",
        )
    verified = await billing.review_transfer(
        "acme",
        order["invoice_id"],
        "operator",
        True,
        "Verified in platform bank",
        platform_operator=True,
        bank_transaction_id="bank-demo",
    )
    assert verified["state"] == "paid"


async def test_one_bank_transaction_cannot_pay_two_invoices(billing: BillingStore) -> None:
    first = await billing.create("acme", ChargeKind.APPOINTMENT, "visit-a", "admin")
    second = await billing.create("acme", ChargeKind.APPOINTMENT, "visit-b", "admin")
    for order in [first, second]:
        await billing.notify_transfer(
            "acme", order["invoice_id"], "reported-demo-reference", "payer"
        )
    await billing.review_transfer(
        "acme",
        first["invoice_id"],
        "admin",
        True,
        "Verified in clinic bank",
        bank_transaction_id="single-bank-transaction",
    )
    with pytest.raises(BillingError, match="already used"):
        await billing.review_transfer(
            "acme",
            second["invoice_id"],
            "admin",
            True,
            "Verified in clinic bank",
            bank_transaction_id="single-bank-transaction",
        )
    rows = await billing.listing("acme", ChargeKind.APPOINTMENT)
    assert sorted(r["state"] for r in rows) == ["paid", "transfer_review"]


async def test_no_unconfigured_price_or_disabled_payment_creates_an_invoice(
    billing: BillingStore,
) -> None:
    billing.settings.billing_monthly_price_cents = 0
    with pytest.raises(BillingError, match="not been configured"):
        await billing.create("acme", ChargeKind.SUBSCRIPTION, None, "admin")
    billing.settings.billing_enabled = False
    with pytest.raises(BillingError, match="not enabled"):
        await billing.create("acme", ChargeKind.APPOINTMENT, "visit-a", "admin")
    assert await billing.listing("acme", ChargeKind.APPOINTMENT) == []


async def test_platform_bank_configuration_requires_operator_scope(billing: BillingStore) -> None:
    with pytest.raises(BillingError):
        await billing.configure_bank("acme", "platform", "Attacker destination account", "admin")
    with pytest.raises(BillingError):
        await billing.configure_bank(
            "acme", "platform", "Attacker destination account", "admin", operator=True
        )
    assert not billing.operator("acme", service=True)
    assert not billing.operator("platform-test", service=False)
    assert billing.operator("platform-test", service=True)


async def test_approved_monthly_price_is_100_dollars_but_payments_stay_closed(
    billing: BillingStore,
) -> None:
    defaults = Settings(_env_file=None, app_env="test")  # type: ignore[call-arg]
    assert defaults.billing_monthly_price_cents == 10_000
    assert not defaults.billing_enabled and not defaults.billing_platform_tenant
    billing.settings.billing_monthly_price_cents = defaults.billing_monthly_price_cents
    invoice = await billing.create("acme", ChargeKind.SUBSCRIPTION, None, "admin")
    assert invoice["amount_cents"] == 10_000 and invoice["currency"] == "USD"


async def test_transfer_correction_can_be_reported_again_but_paid_is_final(
    billing: BillingStore,
) -> None:
    invoice = await billing.create("acme", ChargeKind.APPOINTMENT, "visit-a", "admin")
    iid = invoice["invoice_id"]
    await billing.notify_transfer("acme", iid, "first-reference", "payer")
    await billing.notify_transfer("acme", iid, "first-reference", "payer")
    with pytest.raises(BillingError, match="already being reviewed"):
        await billing.notify_transfer("acme", iid, "different-reference", "payer")
    corrected = await billing.review_transfer(
        "acme", iid, "admin", False, "No matching income in bank"
    )
    assert corrected["state"] == "pending"
    await billing.notify_transfer("acme", iid, "corrected-reference", "payer")
    with pytest.raises(BillingError, match="transaction id"):
        await billing.review_transfer("acme", iid, "admin", True, "Verified in bank")
    await billing.review_transfer(
        "acme", iid, "admin", True, "Verified in bank", bank_transaction_id="actual-bank-id"
    )
    with pytest.raises(BillingError):
        await billing.notify_transfer("acme", iid, "new-reference", "payer")
    with pytest.raises(BillingError):
        await billing.review_transfer("acme", iid, "admin", False, "Cannot undo a payment")


async def test_rights_export_includes_every_own_payment_without_other_clinic_or_subscription(
    billing: BillingStore,
    orchestrator: Orchestrator,
) -> None:
    template = await billing.create("acme", ChargeKind.APPOINTMENT, "visit-a", "admin")
    await billing.create("globex", ChargeKind.APPOINTMENT, "visit-a", "admin")
    await billing.create("acme", ChargeKind.SUBSCRIPTION, None, "admin")
    async with billing.db.engine.begin() as conn:
        await conn.execute(
            insert(orders),
            [
                {
                    "tenant": "acme",
                    "invoice_id": f"{n:032x}",
                    "kind": "appointment",
                    "resource_id": f"historic-{n}",
                    "patient_id": "patient",
                    "merchant_id": "synthetic-merchant",
                    "amount_cents": 100,
                    "currency": "USD",
                    "method": "transfer",
                    "state": "pending",
                    "bank_instructions": "Synthetic instructions",
                    "created_at": utcnow(),
                    "updated_at": utcnow(),
                }
                for n in range(201)
            ],
        )
    exported = await orchestrator.rights.export("acme", "patient", "privacy-officer")
    assert len(exported["payments"]) == 202
    assert template["invoice_id"] in {r["invoice_id"] for r in exported["payments"]}
    assert exported["inventory"]["payments"] == 202
    assert any(e["action"] == "billing.invoice.created" for e in exported["audit"])
    assert {r["kind"] for r in exported["payments"]} == {"appointment"}
    assert all("merchant_id" not in r and "settlement_id" not in r for r in exported["payments"])
    assert await billing.export_subject("acme", "different-patient") == []
