"""Automatic payment checks and WhatsApp deliveries use synthetic transports only."""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from collections.abc import AsyncIterator
from datetime import timedelta
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import insert, update

from orchestrator.api.app import create_app
from orchestrator.billing import BillingError, ChargeKind
from orchestrator.billing_automation import BillingAutomation, targets
from orchestrator.billing_followup import events
from orchestrator.billing_store import orders
from orchestrator.catalog import Catalog
from orchestrator.config import Settings
from orchestrator.crm import appointments, patients
from orchestrator.db import utcnow
from orchestrator.dlocal_billing import DlocalBilling, accounts, checkouts, renewals
from orchestrator.dlocal_go import DlocalGo
from orchestrator.governance import Purpose
from orchestrator.llm import FakeLLM
from orchestrator.service import Orchestrator
from orchestrator.social import channel_accounts


class Provider:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.payment: dict[str, Any] = {}
        self.payments: dict[str, dict[str, Any]] = {}
        self.plan: dict[str, Any] = {}
        self.subscribers: list[dict[str, Any]] = []
        self.executions: list[dict[str, Any]] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/v1/me":
            return httpx.Response(200, json={"id": "fictional-merchant"})
        if path == "/v1/payments" and request.method == "POST":
            self.payment = {
                **json.loads(request.content),
                "id": "DP-synthetic" if not self.payments else "DP-" + str(len(self.payments) + 1),
                "status": "PENDING",
                "status_detail": "PENDING_PAYMENT",
                "redirect_url": "https://checkout-sbx.dlocalgo.com/validate/Synthetic",
            }
            self.payments[self.payment["id"]] = self.payment
            return httpx.Response(200, json=self.payment)
        if path.startswith("/v1/payments/"):
            return httpx.Response(200, json=self.payments[path.rsplit("/", 1)[1]])
        if path == "/v1/subscription/plan" and request.method == "POST":
            self.plan = {
                **json.loads(request.content),
                "id": 91,
                "active": True,
                "free_trial_days": 0,
                "subscribe_url": "https://checkout-sbx.dlocalgo.com/validate/subscription/Synthetic",
            }
            return httpx.Response(200, json=self.plan)
        if path == "/v1/subscription/plan/91":
            return httpx.Response(200, json=self.plan)
        if path.endswith("/subscription/all"):
            return httpx.Response(200, json={"data": self.subscribers, "total_pages": 1})
        if path.endswith("/execution/all"):
            return httpx.Response(200, json={"data": self.executions, "total_pages": 1})
        return httpx.Response(404, json={"error": "fictional endpoint not available"})

    def activate(self) -> None:
        self.subscribers = [{"id": 7, "plan": self.plan, "active": True, "status": "CONFIRMED"}]
        self.executions = [
            {
                "id": 17,
                "subscription": self.subscribers[0],
                "status": "COMPLETED",
                "currency": "USD",
                "checkout_currency": "USD",
                "amount_paid": 100,
            }
        ]


@pytest.fixture
async def auto_world(orchestrator: Orchestrator, monkeypatch: pytest.MonkeyPatch) -> Any:
    return await setup_world(orchestrator, monkeypatch)


async def setup_world(orchestrator: Orchestrator, monkeypatch: pytest.MonkeyPatch) -> Any:
    o = orchestrator
    o.settings.billing_enabled = True
    o.settings.billing_automation_enabled = True
    o.settings.billing_platform_tenant = "globex"
    o.settings.public_base_url = "https://synthetic.example"
    provider = Provider()
    o.dlocal_billing = DlocalBilling(o.billing, DlocalGo(httpx.MockTransport(provider.handle)))
    for tenant, scope in (("acme", "clinic"), ("globex", "platform")):
        await o.dlocal_billing.connect(
            tenant,
            scope,
            "sandbox",
            tenant + "-example",
            "fictional-secret",
            False,
            "admin",
            operator=scope == "platform",
        )
    now = utcnow().replace(hour=15, minute=0, second=0, microsecond=0)
    async with o.db.engine.begin() as conn:
        await conn.execute(
            insert(patients).values(
                tenant="acme",
                id="p1",
                display_name="Fictional patient",
                phone="+12025550123",
                restricted=False,
                created_at=now,
            )
        )
        await conn.execute(
            insert(appointments).values(
                tenant="acme",
                id="visit",
                patient_id="p1",
                starts_at=now,
                duration_min=50,
                kind="follow_up",
                status="scheduled",
                price=Decimal("49.99"),
                created_at=now,
                updated_at=now,
            )
        )
        for tenant, aid in (("acme", "a" * 32), ("globex", "b" * 32)):
            await conn.execute(
                insert(channel_accounts).values(
                    tenant=tenant,
                    account_id=aid,
                    network="whatsapp",
                    external_id="123" if tenant == "acme" else "456",
                    handle="Fictional WhatsApp",
                    secret_ref="DEMO_" + tenant.upper(),
                    audited=False,
                    active=True,
                    created_at=now,
                    created_by="admin",
                )
            )
    monkeypatch.setenv("SOCIAL_SECRET_DEMO_ACME", "fictional-channel")
    monkeypatch.setenv("SOCIAL_SECRET_DEMO_GLOBEX", "fictional-platform")
    deliveries: list[httpx.Request] = []

    def deliver(req: httpx.Request) -> httpx.Response:
        deliveries.append(req)
        return httpx.Response(200, json={"messages": [{"id": "wamid.fictional"}]})

    o.billing_automation = BillingAutomation(o, httpx.MockTransport(deliver))
    await o.billing_automation.configure_policy(
        "acme",
        "admin",
        enabled=True,
        account_id="a" * 32,
        template_name="payment_due",
        language="es",
        cadence_days=3,
        maximum=3,
    )
    await o.consents.record(
        "acme",
        "p1",
        Purpose.WHATSAPP_BILLING,
        True,
        source="Fictional explicit payment permission",
        actor="patient:p1",
    )
    invoice = await o.billing.create("acme", ChargeKind.APPOINTMENT, "visit", "admin")
    await o.dlocal_billing.checkout("acme", invoice["invoice_id"], "patient:p1")
    provider.payment["status_detail"] = "PENDING_PAYMENT"
    await o.billing_automation.configure_order(
        "acme", invoice["invoice_id"], True, now - timedelta(days=1), "admin"
    )
    return o, provider, deliveries, invoice["invoice_id"], now


async def test_verified_pending_sends_once_and_never_confirms_payment(auto_world: Any) -> None:
    o, provider, sent, _fid, now = auto_world
    result = await o.billing_automation.due(now)
    assert result["simulated"] == 1 and result["accepted"] == 0
    assert len(sent) == 1
    await o.billing_automation.due(now + timedelta(minutes=1))
    assert len(sent) == 1
    body = sent[0].read().decode()
    assert "USD 49.99" in body and "Fictional patient" not in body
    assert (await o.billing.listing("acme", ChargeKind.APPOINTMENT))[0]["state"] != "paid"
    await o.billing_automation.due(now + timedelta(minutes=1))
    assert len(sent) == 1
    data = await o.billing.export_subject("acme", "p1")
    assert data[0]["automatic_followup"]["enabled"]
    assert data[0]["whatsapp_followup_history"][0]["state"] == "auto_simulated"
    assert "address_key" not in str(data)
    assert any(
        r.method == "GET" and r.url.path == "/v1/payments/DP-synthetic" for r in provider.requests
    )


async def test_paid_provider_result_stops_reminder_and_updates_app(auto_world: Any) -> None:
    o, provider, sent, fid, now = auto_world
    provider.payment["status"] = "PAID"
    await o.billing_automation.due(now)
    assert not sent
    assert (await o.billing.listing("acme", ChargeKind.APPOINTMENT))[0]["state"] == "paid"
    target = await o.billing_automation.target("acme", "order:" + fid)
    assert not target["enabled"] and target["last_result"] == "paid"


@pytest.mark.parametrize("reason", ["permission", "stop", "pause", "promised", "unknown_payment"])
async def test_automatic_reminders_obey_permissions_and_review(
    auto_world: Any, reason: str
) -> None:
    o, provider, sent, _, now = auto_world
    if reason == "permission":
        await o.consents.record(
            "acme",
            "p1",
            Purpose.WHATSAPP_BILLING,
            False,
            source="Fictional withdrawal",
            actor="patient:p1",
        )
    elif reason == "stop":
        o.inbound.is_opted_out = AsyncMock(return_value=True)
    elif reason in ("pause", "promised"):
        fid = auto_world[3]
        await o.payment_followups.schedule(
            "acme",
            fid,
            "admin",
            paused=reason == "pause",
            promised_for=now + timedelta(days=2) if reason == "promised" else None,
        )
    else:
        provider.payment["status_detail"] = "PROCESSING"
    result = await o.billing_automation.due(now)
    assert result["blocked"] == 1 and not sent


async def test_unavailable_or_mismatched_gateway_cannot_trigger_messages(auto_world: Any) -> None:
    o, provider, sent, _, now = auto_world
    provider.payment["amount"] = 1
    await o.billing_automation.due(now)
    assert not sent


async def test_cadence_cap_and_quiet_hours(auto_world: Any) -> None:
    o, _, sent, _, now = auto_world
    await o.billing_automation.due(now.replace(hour=4))
    assert not sent
    for days in (0, 3, 6, 9):
        await o.billing_automation.due(now + timedelta(days=days))
    assert len(sent) == 3


async def test_owner_daily_rule_starts_next_day_and_stops_at_three(auto_world: Any) -> None:
    o, _, sent, fid, now = auto_world
    await o.billing_automation.configure_policy(
        "acme",
        "admin",
        enabled=True,
        account_id="a" * 32,
        template_name="payment_due",
        language="es",
        cadence_days=1,
        maximum=3,
    )
    await o.billing_automation.configure_order("acme", fid, True, now, "admin")
    await o.billing_automation.due(now)
    assert not sent
    for day in (1, 2, 3, 4):
        await o.billing_automation.due(now + timedelta(days=day))
    assert len(sent) == 3


async def test_due_date_with_local_offset_is_stored_in_utc_and_first_contact_is_nine(
    auto_world: Any,
) -> None:
    o, _, _, fid, now = auto_world
    local = ZoneInfo("America/Guayaquil")
    due = now.astimezone(local).replace(hour=17)
    configured = await o.billing_automation.configure_order("acme", fid, True, due, "admin")
    assert configured["due_at"].hour == 22
    assert configured["next_at"].hour == 14
    assert configured["next_at"].astimezone(local) == (due + timedelta(days=1)).replace(hour=9)


async def test_same_recipient_only_gets_one_reminder_per_sender_per_day(auto_world: Any) -> None:
    o, _, sent, _, now = auto_world
    async with o.db.engine.begin() as conn:
        await conn.execute(
            insert(appointments).values(
                tenant="acme",
                id="visit-two",
                patient_id="p1",
                starts_at=now + timedelta(hours=2),
                duration_min=50,
                kind="follow_up",
                status="scheduled",
                price=Decimal("49.99"),
                created_at=now,
                updated_at=now,
            )
        )
    second = await o.billing.create("acme", ChargeKind.APPOINTMENT, "visit-two", "admin")
    await o.dlocal_billing.checkout("acme", second["invoice_id"], "admin")
    await o.billing_automation.configure_order(
        "acme", second["invoice_id"], True, now - timedelta(days=1), "admin"
    )
    await asyncio.gather(o.billing_automation.due(now), o.billing_automation.due(now))
    assert len(sent) == 1


async def test_two_workers_only_one_send(auto_world: Any) -> None:
    o, _, sent, _, now = auto_world
    await asyncio.gather(o.billing_automation.due(now), o.billing_automation.due(now))
    assert len(sent) == 1


async def test_uncertain_delivery_survives_restart_and_cannot_be_reenabled(auto_world: Any) -> None:
    o, _, sent, fid, now = auto_world

    def timeout(req: httpx.Request) -> httpx.Response:
        sent.append(req)
        raise httpx.ReadTimeout("fictional timeout", request=req)

    o.billing_automation.transport = httpx.MockTransport(timeout)
    await o.billing_automation.due(now)
    o.billing_automation = BillingAutomation(o, httpx.MockTransport(timeout))
    await o.billing_automation.due(now + timedelta(days=4))
    assert len(sent) == 1
    await o.billing_automation.configure_order("acme", fid, False, now, "admin")
    with pytest.raises(BillingError, match="delivery reconciliation"):
        await o.billing_automation.configure_order("acme", fid, True, now, "admin")
    with pytest.raises(BillingError, match="automatic reminders"):
        await o.payment_followups.prepare("acme", fid, "reception")


async def test_global_switch_and_sandbox_cannot_send_real_whatsapp(auto_world: Any) -> None:
    o, _, sent, _, now = auto_world
    o.settings.billing_automation_enabled = False
    await o.billing_automation.due(now)
    assert not sent
    o.settings.billing_automation_enabled = True
    o.billing_automation.transport = None
    result = await o.billing_automation.due(now)
    assert not sent and result["blocked"] == 1


async def test_direct_bank_transfer_is_not_automatically_verified(auto_world: Any) -> None:
    o, _, _, fid, now = auto_world
    async with o.db.engine.begin() as conn:
        await conn.execute(
            update(orders).where(orders.c.invoice_id == fid).values(method="transfer")
        )
    with pytest.raises(BillingError, match="registered with the gateway"):
        await o.billing_automation.configure_order("acme", fid, True, now, "admin")


async def test_live_delivery_requires_live_gate_and_recipient_confirmation(auto_world: Any) -> None:
    o, _, sent, _, now = auto_world
    async with o.db.engine.begin() as conn:
        await conn.execute(update(accounts).values(environment="live", withdrawals_confirmed=True))
    await o.billing_automation.due(now)
    assert not sent
    o.settings.billing_dlocal_live_enabled = True
    async with o.db.engine.begin() as conn:
        await conn.execute(update(targets).values(next_at=now))
    result = await o.billing_automation.due(now)
    assert len(sent) == 1 and result["accepted"] == 1


async def test_other_tenant_cannot_enroll_foreign_invoice_or_sender(auto_world: Any) -> None:
    o, _, _, fid, now = auto_world
    with pytest.raises(KeyError):
        await o.billing_automation.configure_order("globex", fid, True, now, "intruder")
    with pytest.raises(BillingError, match="connect"):
        await o.billing_automation.configure_policy(
            "globex",
            "intruder",
            enabled=True,
            account_id="a" * 32,
            template_name="payment_due",
            language="es",
            cadence_days=3,
            maximum=3,
        )


async def test_manual_draft_and_auto_cannot_both_be_prepared(auto_world: Any) -> None:
    o, _, _, fid, now = auto_world
    await o.billing_automation.configure_order("acme", fid, False, now, "admin")
    async with o.db.engine.begin() as conn:
        await conn.execute(
            insert(events).values(
                tenant="acme",
                invoice_id=fid,
                event_id="c" * 32,
                state="prepared",
                open_slot="open",
                address_key="0" * 64,
                actor="reception",
                created_at=now,
            )
        )
    with pytest.raises(BillingError, match="manual draft"):
        await o.billing_automation.configure_order("acme", fid, True, now, "admin")


async def test_crash_claim_is_uncertain_and_never_repeated(auto_world: Any) -> None:
    o, _, sent, fid, now = auto_world
    async with o.db.engine.begin() as conn:
        await conn.execute(
            update(targets)
            .where(targets.c.source_id == fid)
            .values(state="sending", updated_at=now - timedelta(minutes=11))
        )
    await o.billing_automation.due(now)
    assert not sent
    assert (await o.billing_automation.target("acme", "order:" + fid))["state"] == "uncertain"


async def test_periodic_reconciliation_updates_without_pressing_check(auto_world: Any) -> None:
    o, provider, sent, fid, now = auto_world
    await o.billing_automation.configure_order("acme", fid, False, now, "admin")
    provider.payment["status"] = "PAID"
    async with o.db.engine.begin() as conn:
        await conn.execute(
            update(checkouts)
            .where(checkouts.c.invoice_id == fid)
            .values(updated_at=now - timedelta(minutes=6))
        )
    result = await o.billing_automation.due(now)
    assert result["reconciled"] == 1
    assert (await o.billing.listing("acme", ChargeKind.APPOINTMENT))[0]["state"] == "paid"
    assert not sent


async def test_native_monthly_failure_and_later_payment_stop_notifications(auto_world: Any) -> None:
    o, provider, sent, fid, now = auto_world
    await o.billing_automation.configure_order("acme", fid, False, now, "admin")
    await o.billing_automation.configure_policy(
        "globex",
        "operator",
        platform=True,
        enabled=True,
        account_id="b" * 32,
        template_name="payment_due",
        language="es",
        cadence_days=3,
        maximum=3,
    )
    await o.payment_followups.configure_contact(
        "acme", "+12025550124", True, "Fictional monthly permission", "admin"
    )
    await o.dlocal_billing.subscribe("acme", "admin", True)
    provider.activate()
    provider.executions[0].update(status="DECLINED", created_at="2026-11-03T09:00:00")
    await o.dlocal_billing.sync_subscription("acme")
    assert (await o.billing_automation.due(now))["simulated"] == 0
    result = await o.billing_automation.due(now + timedelta(days=1))
    assert result["simulated"] == 1 and len(sent) == 1
    assert sent[0].url.path == "/v25.0/456/messages"
    provider.executions.append(
        {
            **provider.executions[0],
            "id": 18,
            "status": "COMPLETED",
            "created_at": "2026-11-04T09:00:00",
        }
    )
    await o.billing_automation.due(now + timedelta(days=3))
    assert len(sent) == 1
    assert (await o.billing_automation.target("acme", "renewal:17"))[
        "last_result"
    ] == "renewal_no_longer_pending"


async def test_renewal_without_ordered_timestamps_cannot_imply_latest_debt(auto_world: Any) -> None:
    o, _, _, _, _ = auto_world
    async with o.db.engine.begin() as conn:
        await conn.execute(
            insert(renewals).values(
                tenant="acme",
                execution_id="x",
                subscription_id="s",
                state="rejected",
                amount_cents=None,
                updated_at=utcnow(),
                provider_created_at=None,
            )
        )
    assert await o.billing_automation.latest_renewal("acme") is None


def test_http_policy_roles_limits_and_scope(settings: Settings, catalog: Catalog) -> None:
    settings.billing_platform_tenant = "globex"
    o = Orchestrator(settings, catalog=catalog, llm=FakeLLM())
    with TestClient(create_app(settings, o)) as client:
        assert client.get("/v1/billing/automation").status_code == 401
        clinic = {"X-API-Key": "test-key"}
        platform = {"X-API-Key": "other-key"}
        assert client.get("/v1/billing/automation", headers=clinic).status_code == 200
        assert (
            client.put(
                "/v1/billing/automation", headers=clinic, json={"enabled": False, "cadence_days": 2}
            ).status_code
            == 200
        )
        assert client.get("/v1/billing/automation", headers=platform).json()["cadence_days"] == 1
        assert client.get("/v1/billing/platform/automation", headers=clinic).status_code == 403
        assert (
            client.put(
                "/v1/billing/platform/automation", headers=clinic, json={"enabled": False}
            ).status_code
            == 403
        )
        assert client.get("/v1/billing/platform/automation", headers=platform).status_code == 200
        assert (
            client.put(
                "/v1/billing/orders/" + "a" * 32 + "/automation",
                headers=platform,
                json={"enabled": True, "due_at": "2026-11-03T10:00:00-05:00"},
            ).status_code
            == 404
        )
        assert (
            client.put(
                "/v1/billing/platform/acme/automation/17", headers=clinic, json={"enabled": False}
            ).status_code
            == 403
        )
        assert (
            client.put("/v1/billing/automation", headers=clinic, json={"maximum": 4}).status_code
            == 422
        )
        assert (
            client.put(
                "/v1/billing/automation", headers=clinic, json={"cadence_days": 0}
            ).status_code
            == 422
        )


@pytest.fixture
async def postgres_world(
    settings: Settings, catalog: Catalog, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[Any]:
    url = os.environ.get("TEST_POSTGRES_URL")
    if not url:
        pytest.skip("set TEST_POSTGRES_URL for real cross-replica reminder concurrency")
    import psycopg
    from psycopg import sql

    name = "billing_auto_" + uuid.uuid4().hex[:12]
    admin = url.rsplit("/", 1)[0] + "/postgres"
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    pods = []
    try:
        settings.database_url = SecretStr(
            url.rsplit("/", 1)[0].replace("postgresql://", "postgresql+psycopg://") + "/" + name
        )
        settings.db_auto_migrate = True
        for _ in range(2):
            pod = Orchestrator(settings, catalog=catalog, llm=FakeLLM())
            await pod.start()
            pods.append(pod)
        world = await setup_world(pods[0], monkeypatch)
        pods[1].dlocal_billing = DlocalBilling(pods[1].billing, pods[0].dlocal_billing.gateway)
        pods[1].billing_automation = BillingAutomation(
            pods[1], pods[0].billing_automation.transport
        )
        yield world, pods[1]
    finally:
        for pod in pods:
            await pod.close()
        with psycopg.connect(admin, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


async def test_postgres_two_replicas_do_not_send_duplicate_reminders(postgres_world: Any) -> None:
    (a, _, sent, _, now), b = postgres_world
    await asyncio.gather(a.billing_automation.due(now), b.billing_automation.due(now))
    assert len(sent) == 1
    await asyncio.gather(
        a.billing_automation.due(now + timedelta(minutes=1)),
        b.billing_automation.due(now + timedelta(minutes=1)),
    )
    assert len(sent) == 1
