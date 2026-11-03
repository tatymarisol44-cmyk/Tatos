"""Invoice courtesy reminders. No WhatsApp network requests are made by these tests."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import insert, select, update

from orchestrator.api.app import create_app
from orchestrator.billing import BillingError, ChargeKind
from orchestrator.billing_followup import contacts, events, phone_number
from orchestrator.billing_store import orders
from orchestrator.catalog import Catalog
from orchestrator.config import Settings
from orchestrator.crm import appointments, patients
from orchestrator.db import aware, utcnow
from orchestrator.dlocal_go import GatewayError
from orchestrator.governance import Purpose
from orchestrator.inbound import address_key, channel_optouts
from orchestrator.llm import FakeLLM
from orchestrator.sealing import SealError
from orchestrator.service import Orchestrator

PHONE = "12025550123"  # reserved fictional NANP number


@pytest.fixture
async def world(orchestrator: Orchestrator) -> tuple[Orchestrator, dict[str, str]]:
    o = orchestrator
    o.settings.billing_enabled = True
    o.settings.billing_platform_tenant = "platform"
    await o.billing.configure_bank(
        "platform", "platform", "Fictional Tatos bank instructions", "system", operator=True
    )
    for tenant in ("acme", "globex"):
        await o.billing.configure_bank(
            tenant, "clinic", "Fictional clinic bank instructions", "admin"
        )
        async with o.db.engine.begin() as conn:
            for subject in ("p1", "p2"):
                await conn.execute(
                    insert(patients).values(
                        tenant=tenant,
                        id=subject,
                        display_name="Ficticia " + subject,
                        phone="+1 (202) 555-0123",
                        restricted=False,
                        created_at=utcnow(),
                    )
                )
                await conn.execute(
                    insert(appointments).values(
                        tenant=tenant,
                        id="visit-" + subject,
                        patient_id=subject,
                        starts_at=utcnow() + timedelta(days=1),
                        duration_min=50,
                        kind="follow_up",
                        status="scheduled",
                        price=Decimal("49.99"),
                        created_at=utcnow(),
                        updated_at=utcnow(),
                    )
                )
    ids = {}
    for tenant, subject in [("acme", "p1"), ("acme", "p2"), ("globex", "p1")]:
        invoice = await o.billing.create(
            tenant, ChargeKind.APPOINTMENT, "visit-" + subject, "admin"
        )
        ids[f"{tenant}-{subject}"] = invoice["invoice_id"]
    for tenant in ("acme", "globex"):
        invoice = await o.billing.create(tenant, ChargeKind.SUBSCRIPTION, None, "admin")
        ids[tenant + "-subscription"] = invoice["invoice_id"]
    return o, ids


async def allow(o: Orchestrator, subject: str = "p1") -> None:
    await o.consents.record(
        "acme",
        subject,
        Purpose.WHATSAPP_BILLING,
        True,
        source="Fictional written opt-in",
        actor="reception",
    )


async def test_marketing_is_not_billing_permission_and_preview_is_not_delivery(world: Any) -> None:
    o, ids = world
    fid = ids["acme-p1"]
    await o.consents.record(
        "acme",
        "p1",
        Purpose.MARKETING,
        True,
        source="Fictional marketing permission",
        actor="reception",
    )
    with pytest.raises(BillingError, match="permission_missing"):
        await o.payment_followups.prepare("acme", fid, "reception")
    await allow(o)
    draft = await o.payment_followups.prepare("acme", fid, "reception")
    assert draft["state"] == "prepared" and draft["sent"] is False
    url = urlsplit(draft["whatsapp_url"])
    assert url.scheme == "https" and url.netloc == "wa.me" and url.path == "/" + PHONE
    message = parse_qs(url.query)["text"][0]
    assert "USD 49.99" in message and fid[:8] in message
    assert "Ficticia" not in message and "cita" not in message and "diagnóstico" not in message
    retry = await o.payment_followups.prepare("acme", fid, "reception")
    assert retry["event_id"] == draft["event_id"]
    row = (await o.payment_followups.listing("acme"))["items"]
    item = next(r for r in row if r["invoice_id"] == fid)
    assert item["history"][0]["state"] == "prepared"
    assert PHONE not in str(row)
    assert item["state"] == "pending"


async def test_only_explicit_manual_report_records_contact_and_never_pays(world: Any) -> None:
    o, ids = world
    await allow(o)
    fid = ids["acme-p1"]
    draft = await o.payment_followups.prepare("acme", fid, "reception")
    for _ in range(2):
        result = await o.payment_followups.complete(
            "acme", fid, draft["event_id"], "reception", True
        )
        assert result == {"state": "manual_sent", "delivery_verified": False}
    with pytest.raises(BillingError, match="contact_not_due"):
        await o.payment_followups.prepare("acme", fid, "reception")
    invoices = await o.billing.listing("acme", ChargeKind.APPOINTMENT)
    assert next(r for r in invoices if r["invoice_id"] == fid)["state"] == "pending"
    data = await o.billing.export_subject("acme", "p1")
    assert data[0]["whatsapp_followup_history"][0]["state"] == "manual_sent"
    assert "address_key" not in str(data) and PHONE not in str(data)
    assert data[0]["followup"]["next_contact_at"]
    assert all(row["invoice_id"] != ids["acme-p2"] for row in data)


async def test_discarding_does_not_claim_contact_or_block_a_new_draft(world: Any) -> None:
    o, ids = world
    await allow(o)
    fid = ids["acme-p1"]
    draft = await o.payment_followups.prepare("acme", fid, "reception")
    await o.payment_followups.complete("acme", fid, draft["event_id"], "reception", False)
    new = await o.payment_followups.prepare("acme", fid, "reception")
    assert new["event_id"] != draft["event_id"]
    with pytest.raises(BillingError, match="already closed"):
        await o.payment_followups.complete("acme", fid, draft["event_id"], "reception", True)


@pytest.mark.parametrize(
    "state", ["paid", "transfer_review", "processing", "uncertain", "cancelled"]
)
async def test_paid_or_unverified_payments_are_not_reminded(world: Any, state: str) -> None:
    o, ids = world
    await allow(o)
    fid = ids["acme-p1"]
    async with o.db.engine.begin() as conn:
        await conn.execute(update(orders).where(orders.c.invoice_id == fid).values(state=state))
    with pytest.raises(BillingError):
        await o.payment_followups.prepare("acme", fid, "reception")
    rows = (await o.payment_followups.listing("acme"))["items"]
    if state == "paid":
        assert all(r["invoice_id"] != fid for r in rows)
    else:
        assert (
            next(r for r in rows if r["invoice_id"] == fid)["blocked_reason"]
            == "payment_needs_review"
        )


async def test_stale_draft_cannot_be_prepared_again_after_payment(world: Any) -> None:
    o, ids = world
    await allow(o)
    fid = ids["acme-p1"]
    draft = await o.payment_followups.prepare("acme", fid, "reception")
    async with o.db.engine.begin() as conn:
        await conn.execute(update(orders).where(orders.c.invoice_id == fid).values(state="paid"))
    with pytest.raises(BillingError, match="paid"):
        await o.payment_followups.prepare("acme", fid, "reception")
    # A historic, explicit manual report may still be recorded without changing payment.
    await o.payment_followups.complete("acme", fid, draft["event_id"], "reception", True)
    assert (
        next(
            r
            for r in await o.billing.listing("acme", ChargeKind.APPOINTMENT)
            if r["invoice_id"] == fid
        )["state"]
        == "paid"
    )


@pytest.mark.parametrize("reason", ["paused", "payment_promised", "contact_not_due"])
async def test_pause_promise_and_scheduled_followup_are_enforced(world: Any, reason: str) -> None:
    o, ids = world
    await allow(o)
    fid = ids["acme-p1"]
    options = (
        {"paused": True}
        if reason == "paused"
        else {
            "promised_for" if reason == "payment_promised" else "next_contact_at": utcnow()
            + timedelta(days=7)
        }
    )
    await o.payment_followups.schedule("acme", fid, "reception", **options)
    with pytest.raises(BillingError, match=reason):
        await o.payment_followups.prepare("acme", fid, "reception")
    await o.payment_followups.schedule("acme", fid, "reception")
    assert (await o.payment_followups.prepare("acme", fid, "reception"))["state"] == "prepared"


async def test_stop_and_withdrawal_and_restricted_patient_block_contact(world: Any) -> None:
    o, ids = world
    await allow(o)
    fid = ids["acme-p1"]
    async with o.db.engine.begin() as conn:
        await conn.execute(
            insert(channel_optouts).values(
                tenant="acme",
                network="whatsapp",
                address_key=address_key(o.settings.pseudonym_secret(), "acme", "whatsapp", PHONE),
                created_at=utcnow(),
            )
        )
    with pytest.raises(BillingError, match="opted_out"):
        await o.payment_followups.prepare("acme", fid, "reception")
    await o.consents.record(
        "acme",
        "p1",
        Purpose.WHATSAPP_BILLING,
        False,
        source="Fictional withdrawal",
        actor="patient:p1",
    )
    with pytest.raises(BillingError, match="permission_missing"):
        await o.payment_followups.prepare("acme", fid, "reception")
    async with o.db.engine.begin() as conn:
        await conn.execute(
            update(patients).where(patients.c.tenant == "acme").values(restricted=True)
        )
    assert (await o.payment_followups.listing("acme"))["items"][0][
        "blocked_reason"
    ] == "inactive_record"


async def test_other_tenant_cannot_prepare_schedule_or_confirm(world: Any) -> None:
    o, ids = world
    await allow(o)
    fid = ids["acme-p1"]
    draft = await o.payment_followups.prepare("acme", fid, "reception")
    for operation in [
        o.payment_followups.prepare("globex", fid, "intruder"),
        o.payment_followups.schedule("globex", fid, "intruder"),
        o.payment_followups.complete("globex", fid, draft["event_id"], "intruder", True),
    ]:
        with pytest.raises(KeyError):
            await operation
    assert all(
        r["invoice_id"] != fid for r in (await o.payment_followups.listing("globex"))["items"]
    )


async def test_monthly_contact_is_sealed_and_belongs_to_its_clinic(world: Any) -> None:
    o, ids = world
    await o.payment_followups.configure_contact(
        "acme", "+1 (202) 555-0123", True, "Fictional administrator opt-in", "admin"
    )
    listing = await o.payment_followups.listing("platform", platform=True)
    row = next(r for r in listing["items"] if r["payer_tenant"] == "acme")
    assert row["blocked_reason"] is None and row["kind"] == "subscription"
    blocked = next(r for r in listing["items"] if r["payer_tenant"] == "globex")
    assert blocked["blocked_reason"] == "phone_missing"
    draft = await o.payment_followups.prepare(
        "acme", ids["acme-subscription"], "operator", platform=True
    )
    assert "mensualidad de Tatos" in draft["message"] and "USD 100.00" in draft["message"]
    async with o.db.engine.connect() as conn:
        saved = (
            (await conn.execute(select(contacts).where(contacts.c.tenant == "acme")))
            .mappings()
            .one()
        )
        assert PHONE not in str(saved)
    with pytest.raises(SealError):
        o.payment_followups.sealer.open("globex", saved["phone_sealed"])
    await o.payment_followups.configure_contact(
        "acme", None, False, "Fictional withdrawal", "admin"
    )
    with pytest.raises(BillingError, match="permission_missing"):
        await o.payment_followups.prepare(
            "acme", ids["acme-subscription"], "operator", platform=True
        )


async def test_card_reconciliation_runs_before_reminder_and_blocks_uncertainty(
    world: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    o, ids = world
    await allow(o)
    fid = ids["acme-p1"]
    async with o.db.engine.begin() as conn:
        await conn.execute(
            update(orders).where(orders.c.invoice_id == fid).values(method="card", state="rejected")
        )
    verify = AsyncMock(side_effect=GatewayError("payment is uncertain"))
    monkeypatch.setattr(o.dlocal_billing, "reconcile_payment", verify)
    with pytest.raises(GatewayError):
        await o.payment_followups.prepare("acme", fid, "reception")
    verify.assert_awaited_once_with("acme", fid)
    async with o.db.engine.connect() as conn:
        assert (await conn.execute(select(events))).first() is None


async def test_explicit_later_followup_date_is_not_shortened_by_contact_report(world: Any) -> None:
    o, ids = world
    await allow(o)
    fid = ids["acme-p1"]
    draft = await o.payment_followups.prepare("acme", fid, "reception")
    later = utcnow() + timedelta(days=14)
    await o.payment_followups.schedule("acme", fid, "reception", next_contact_at=later)
    await o.payment_followups.complete("acme", fid, draft["event_id"], "reception", True)
    row = next(
        r for r in (await o.payment_followups.listing("acme"))["items"] if r["invoice_id"] == fid
    )
    assert aware(row["next_contact_at"]) == later


@pytest.mark.parametrize(
    "value", [None, "0999999999", "javascript:123", "+1/2025550123", "+1202", "+" + "1" * 16]
)
def test_international_phone_is_required_without_guessing_country(value: str | None) -> None:
    assert phone_number(value) is None


async def test_followup_pagination_keeps_every_invoice_in_its_tenant(world: Any) -> None:
    o, ids = world
    async with o.db.engine.begin() as conn:
        original = dict(
            (await conn.execute(select(orders).where(orders.c.invoice_id == ids["acme-p1"])))
            .mappings()
            .one()
        )
        for index in range(1, 106):
            await conn.execute(
                insert(orders).values(
                    **{
                        **original,
                        "invoice_id": f"{index:032x}",
                        "resource_id": f"synthetic-{index}",
                    }
                )
            )
    first = await o.payment_followups.listing("acme")
    assert len(first["items"]) == 100 and first["next_cursor"]
    second = await o.payment_followups.listing("acme", after=first["next_cursor"])
    assert len(second["items"]) == 7 and second["next_cursor"] is None
    assert all(r["payer_tenant"] == "acme" for r in first["items"] + second["items"])
    assert len({r["invoice_id"] for r in first["items"] + second["items"]}) == 107


@pytest.fixture
def http_client(settings: Settings, catalog: Catalog) -> Iterator[TestClient]:
    settings.billing_enabled = True
    settings.billing_platform_tenant = "globex"
    o = Orchestrator(settings, catalog=catalog, llm=FakeLLM())
    with TestClient(create_app(settings, o)) as client:
        yield client


def test_http_roles_and_patient_permission_and_manual_confirmation(http_client: TestClient) -> None:
    client = http_client
    admin = {"X-API-Key": "test-key"}
    other = {"X-API-Key": "other-key"}
    assert (
        client.put(
            "/v1/billing/bank", headers=admin, json={"instructions": "Fictional receiver bank"}
        ).status_code
        == 200
    )
    patient = client.post(
        "/v1/crm/patients",
        headers=admin,
        json={"id": "followup-patient", "display_name": "Ficticia", "phone": "+12025550123"},
    )
    assert patient.status_code == 201, patient.text
    appointment = client.post(
        "/v1/crm/appointments",
        headers=admin,
        json={
            "patient_id": "followup-patient",
            "starts_at": (utcnow() + timedelta(days=1)).isoformat(),
            "duration_min": 50,
            "kind": "follow_up",
            "price": 49.99,
        },
    )
    assert appointment.status_code == 201, appointment.text
    invoice = client.post(f"/v1/billing/appointments/{appointment.json()['id']}", headers=admin)
    assert invoice.status_code == 200, invoice.text
    fid = invoice.json()["invoice_id"]
    base = f"/v1/billing/orders/{fid}"
    assert client.post(base + "/whatsapp", headers=other).status_code == 404
    assert client.post(base + "/whatsapp", headers=admin).status_code == 409
    access = client.post("/v1/crm/patients/followup-patient/access", headers=admin).json()
    own = {"X-API-Key": access["key"]}
    assert client.get("/v1/billing/followups", headers=own).status_code == 403
    assert (
        client.put(
            "/v1/me/consents/whatsapp_billing", headers=own, json={"granted": True}
        ).status_code
        == 200
    )
    listing = client.get("/v1/billing/followups", headers=admin)
    assert listing.status_code == 200 and listing.headers["cache-control"] == "no-store"
    assert listing.json()["items"][0]["blocked_reason"] is None
    draft = client.post(base + "/whatsapp", headers=admin)
    assert draft.status_code == 200 and draft.json()["sent"] is False
    eid = draft.json()["event_id"]
    assert (
        client.post(base + f"/whatsapp/{eid}", headers=own, json={"sent": True}).status_code == 403
    )
    confirmed = client.post(base + f"/whatsapp/{eid}", headers=admin, json={"sent": True})
    assert confirmed.status_code == 200 and confirmed.json()["delivery_verified"] is False
    assert client.get("/v1/billing/orders", headers=admin).json()[0]["state"] == "pending"
    assert client.get("/v1/billing/platform/followups", headers=admin).status_code == 403
    assert client.get("/v1/billing/platform/followups", headers=other).status_code == 200
