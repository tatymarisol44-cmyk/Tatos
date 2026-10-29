"""Hosted dLocal Go flows against a synthetic transport, never a financial account."""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Iterator
from datetime import timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import insert, select

from orchestrator.api.app import create_app
from orchestrator.billing import BillingError, ChargeKind
from orchestrator.catalog import Catalog
from orchestrator.config import Settings
from orchestrator.crm import appointments, patients
from orchestrator.db import utcnow
from orchestrator.dlocal_billing import DlocalBilling, accounts, checkouts, plans
from orchestrator.dlocal_go import Credentials, DlocalGo, GatewayError, validate_payment
from orchestrator.llm import FakeLLM
from orchestrator.sealing import SealError
from orchestrator.service import Orchestrator

KEY = "synthetic-api-key-clinic"
SECRET = "synthetic-secret-clinic"
PLATFORM_KEY = "synthetic-api-key-platform"
PLATFORM_SECRET = "synthetic-secret-platform"
ACME = {"X-API-Key": "test-key"}
GLOBEX = {"X-API-Key": "other-key"}


class Provider:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.payment: dict[str, Any] = {}
        self.plan: dict[str, Any] = {}
        self.subscribers: list[dict[str, Any]] = []
        self.executions: list[dict[str, Any]] = []
        self.timeout = False

    def handle(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        path = req.url.path
        if path == "/v1/me":
            return httpx.Response(200, json={"id": "synthetic-merchant"})
        if self.timeout and req.method == "POST":
            raise httpx.ReadTimeout("synthetic timeout", request=req)
        if path == "/v1/payments" and req.method == "POST":
            self.payment = {
                **json.loads(req.content),
                "id": "DP-synthetic",
                "status": "PENDING",
                "redirect_url": "https://checkout-sbx.dlocalgo.com/validate/Synthetic",
            }
            return httpx.Response(200, json=self.payment)
        if path == "/v1/payments/DP-synthetic":
            return httpx.Response(200, json=self.payment)
        if path == "/v1/subscription/plan" and req.method == "POST":
            self.plan = {
                **json.loads(req.content),
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
        if path.endswith("/deactivate"):
            if path == "/v1/subscription/plan/91/deactivate":
                self.plan["active"] = False
                for sub in self.subscribers:
                    sub["active"] = False
                return httpx.Response(200, json=self.plan)
            for sub in self.subscribers:
                sub["active"] = False
            return httpx.Response(200, json={"id": 7, "active": False, "plan": self.plan})
        return httpx.Response(404, json={"error": "synthetic not found"})

    def signature(self, payload: bytes, *, platform: bool = False) -> str:
        key, secret = (PLATFORM_KEY, PLATFORM_SECRET) if platform else (KEY, SECRET)
        signature = hmac.new(secret.encode(), key.encode() + payload, hashlib.sha256).hexdigest()
        return f"V2-HMAC-SHA256, Signature: {signature}"

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
async def configured(
    orchestrator: Orchestrator, settings: Settings
) -> tuple[DlocalBilling, Provider]:
    settings.billing_enabled = True
    settings.billing_platform_tenant = "globex"
    settings.public_base_url = "https://synthetic.example"
    provider = Provider()
    gateway = DlocalBilling(orchestrator.billing, DlocalGo(httpx.MockTransport(provider.handle)))
    await gateway.connect("acme", "clinic", "sandbox", KEY, SECRET, False, "admin")
    await gateway.connect(
        "globex",
        "platform",
        "sandbox",
        PLATFORM_KEY,
        PLATFORM_SECRET,
        False,
        "operator",
        operator=True,
    )
    now = utcnow()
    async with orchestrator.db.engine.begin() as conn:
        await conn.execute(
            insert(patients).values(
                tenant="acme",
                id="subject",
                display_name="Ficticia",
                restricted=False,
                created_at=now,
            )
        )
        await conn.execute(
            insert(appointments).values(
                tenant="acme",
                id="visit",
                patient_id="subject",
                starts_at=now + timedelta(days=1),
                duration_min=50,
                kind="follow_up",
                status="scheduled",
                price=Decimal("49.99"),
                created_at=now,
                updated_at=now,
            )
        )
    return gateway, provider


async def invoice(gateway: DlocalBilling) -> dict[str, Any]:
    return await gateway.billing.create("acme", ChargeKind.APPOINTMENT, "visit", "admin")


async def test_account_scope_credentials_sealed_and_no_secret_readback(configured: Any) -> None:
    gateway, _ = configured
    async with gateway.db.engine.connect() as conn:
        row = (
            (await conn.execute(select(accounts).where(accounts.c.tenant == "acme")))
            .mappings()
            .one()
        )
    assert KEY not in str(row) and SECRET not in str(row)
    with pytest.raises(SealError):
        gateway.sealer.open("globex", row["credentials_sealed"])
    public = await gateway.configuration("acme")
    assert public == {
        "clinic": {"environment": "sandbox", "available": True},
        "platform": {"environment": "sandbox", "available": True},
    }
    assert KEY not in json.dumps(public)
    with pytest.raises(BillingError, match="operator"):
        await gateway.connect("acme", "platform", "sandbox", KEY, SECRET, False, "admin")
    with pytest.raises(BillingError, match="already belongs"):
        await gateway.connect("globex", "clinic", "sandbox", KEY, SECRET, False, "admin")


async def test_checkout_uses_invoice_amount_and_only_clinic_recipient(configured: Any) -> None:
    gateway, provider = configured
    order = await invoice(gateway)
    checkout = await gateway.checkout("acme", order["invoice_id"], "patient", patient_id="subject")
    outbound = provider.requests[-1]
    assert outbound.headers["authorization"] == f"Bearer {KEY}:{SECRET}"
    data = json.loads(outbound.content)
    assert data["amount"] == 49.99 and data["country"] == "EC" and data["currency"] == "USD"
    assert "subject" not in outbound.content.decode() and "visit" not in outbound.content.decode()
    assert data["description"] == "Pago de servicio"
    assert checkout["state"] == "processing" and "checkout-sbx" in checkout["checkout_url"]
    await gateway.checkout("acme", order["invoice_id"], "patient", patient_id="subject")
    assert sum(r.method == "POST" and r.url.path == "/v1/payments" for r in provider.requests) == 1
    assert (await gateway.billing.listing("acme", ChargeKind.APPOINTMENT))[0]["state"] != "paid"


@pytest.mark.parametrize("tenant,subject", [("globex", "subject"), ("acme", "someone-else")])
async def test_foreign_patients_cannot_start_or_reconcile(
    configured: Any, tenant: str, subject: str
) -> None:
    gateway, provider = configured
    order = await invoice(gateway)
    before = len(provider.requests)
    with pytest.raises(KeyError):
        await gateway.checkout(tenant, order["invoice_id"], "intruder", patient_id=subject)
    with pytest.raises(KeyError):
        await gateway.reconcile_payment(tenant, order["invoice_id"], patient_id=subject)
    assert len(provider.requests) == before


async def test_signed_notification_requires_authenticated_verified_amount_and_is_idempotent(
    configured: Any,
) -> None:
    gateway, provider = configured
    order = await invoice(gateway)
    await gateway.checkout("acme", order["invoice_id"], "admin")
    async with gateway.db.engine.connect() as conn:
        ref = (await conn.execute(select(checkouts.c.reference))).scalar_one()
    payload = b'{"payment_id":"DP-synthetic"}'
    provider.payment["status"] = "PAID"
    with pytest.raises(PermissionError):
        await gateway.webhook(ref, payload, "invalid")
    provider.payment["amount"] = 1
    with pytest.raises(GatewayError, match="does not match"):
        await gateway.webhook(ref, payload, provider.signature(payload))
    provider.payment["amount"] = 49.99
    for _ in range(2):
        await gateway.webhook(ref, payload, provider.signature(payload))
    assert (await gateway.billing.listing("acme", ChargeKind.APPOINTMENT))[0]["state"] == "paid"
    provider.payment["status"] = "PENDING"
    assert (await gateway.reconcile_payment("acme", order["invoice_id"]))["state"] == "paid"


async def test_timeout_reserves_order_and_never_repeats_creation(configured: Any) -> None:
    gateway, provider = configured
    provider.timeout = True
    order = await invoice(gateway)
    with pytest.raises(GatewayError, match="uncertain"):
        await gateway.checkout("acme", order["invoice_id"], "admin")
    retry = await gateway.checkout("acme", order["invoice_id"], "admin")
    assert retry == {"state": "uncertain", "checkout_url": None}
    assert sum(r.method == "POST" for r in provider.requests) == 1
    with pytest.raises(BillingError):
        await gateway.billing.notify_transfer("acme", order["invoice_id"], "bank-test", "payer")


async def test_live_gate_approval_and_global_release_switch(configured: Any) -> None:
    gateway, _ = configured
    with pytest.raises(BillingError, match="approval"):
        await gateway.connect("acme", "clinic", "live", KEY, SECRET, False, "admin")
    await gateway.connect(
        "new-clinic", "clinic", "live", "synthetic-key-live", SECRET, True, "admin"
    )
    assert not (await gateway.configuration("new-clinic"))["clinic"]["available"]
    async with gateway.db.engine.connect() as conn:
        row = (
            (await conn.execute(select(accounts).where(accounts.c.tenant == "new-clinic")))
            .mappings()
            .one()
        )
    with pytest.raises(BillingError, match="not enabled"):
        gateway._credentials(row, for_creation=True)


async def test_monthly_provider_plan_requires_consent_and_only_platform_account(
    configured: Any,
) -> None:
    gateway, provider = configured
    with pytest.raises(BillingError, match="consent"):
        await gateway.subscribe("acme", "admin", False)
    result = await gateway.subscribe("acme", "admin", True)
    data = json.loads(provider.requests[-1].content)
    assert data["amount"] == 100 and data["frequency_type"] == "MONTHLY"
    assert data["frequency_value"] == 1
    assert (
        provider.requests[-1].headers["authorization"] == f"Bearer {PLATFORM_KEY}:{PLATFORM_SECRET}"
    )
    assert result["state"] == "awaiting"
    await gateway.subscribe("acme", "admin", True)
    assert sum(r.method == "POST" for r in provider.requests) == 1
    with pytest.raises(BillingError, match="card plan"):
        await gateway.billing.create("acme", ChargeKind.SUBSCRIPTION, None, "admin")
    provider.activate()
    synced = await gateway.sync_subscription("acme")
    assert synced["state"] == "active" and synced["renewals"][0]["state"] == "paid"
    assert synced["renewals"][0]["amount_cents"] == 10_000
    await gateway.sync_subscription("acme")
    assert len((await gateway.subscription("acme"))["renewals"]) == 1
    assert (await gateway.subscription("globex"))["state"] == "not_started"
    cancelled = await gateway.sync_subscription("acme", cancel=True)
    assert cancelled["state"] == "cancelled"
    assert any(
        r.method == "PATCH" and r.url.path == "/v1/subscription/plan/91/deactivate"
        for r in provider.requests
    )


async def test_subscription_plan_tampering_and_fake_renewals_do_not_settle(configured: Any) -> None:
    gateway, provider = configured
    await gateway.subscribe("acme", "admin", True)
    provider.activate()
    provider.executions[0]["amount_paid"] = 1
    with pytest.raises(GatewayError, match="amount"):
        await gateway.sync_subscription("acme")
    assert (await gateway.subscription("acme"))["renewals"] == []
    provider.executions[0]["amount_paid"] = 100
    provider.plan["frequency_type"] = "DAILY"
    with pytest.raises(GatewayError, match="monthly"):
        await gateway.sync_subscription("acme")


async def test_subscribe_timeout_cannot_create_a_second_plan(configured: Any) -> None:
    gateway, provider = configured
    provider.timeout = True
    with pytest.raises(GatewayError):
        await gateway.subscribe("acme", "admin", True)
    assert (await gateway.subscribe("acme", "admin", True))["state"] == "uncertain"
    assert sum(r.method == "POST" for r in provider.requests) == 1
    async with gateway.db.engine.connect() as conn:
        row = (await conn.execute(select(plans))).mappings().one()
    assert row["plan_id"] is None


async def test_cancel_closes_unused_link_even_with_no_subscriber(configured: Any) -> None:
    gateway, provider = configured
    await gateway.subscribe("acme", "admin", True)
    cancelled = await gateway.sync_subscription("acme", cancel=True)
    assert cancelled["state"] == "cancelled" and provider.plan["active"] is False
    assert (await gateway.subscribe("acme", "admin", True))["checkout_url"] is None


async def test_cancel_is_recorded_without_requiring_old_renewals_to_reconcile(
    configured: Any,
) -> None:
    gateway, provider = configured
    await gateway.subscribe("acme", "admin", True)
    provider.activate()
    provider.executions[0]["amount_paid"] = 1
    assert (await gateway.sync_subscription("acme", cancel=True))["state"] == "cancelled"


async def test_inactive_created_subscription_is_awaiting_payment(configured: Any) -> None:
    gateway, provider = configured
    await gateway.subscribe("acme", "admin", True)
    provider.subscribers = [{"id": 7, "plan": provider.plan, "active": False, "status": "CREATED"}]
    assert (await gateway.sync_subscription("acme"))["state"] == "awaiting"


async def test_uncertain_payment_can_recover_via_signed_hook(configured: Any) -> None:
    gateway, provider = configured
    order = await invoice(gateway)
    provider.timeout = True
    with pytest.raises(GatewayError):
        await gateway.checkout("acme", order["invoice_id"], "admin")
    async with gateway.db.engine.connect() as conn:
        ref = (await conn.execute(select(checkouts.c.reference))).scalar_one()
    provider.payment = {
        "id": "DP-synthetic",
        "order_id": ref,
        "amount": 49.99,
        "country": "EC",
        "currency": "USD",
        "status": "PAID",
    }
    payload = b'{"payment_id":"DP-synthetic"}'
    await gateway.webhook(ref, payload, provider.signature(payload))
    assert (await gateway.billing.listing("acme", ChargeKind.APPOINTMENT))[0]["state"] == "paid"


@pytest.mark.parametrize(
    "url",
    [
        "http://checkout-sbx.dlocalgo.com/validate/abc",
        "https://checkout.dlocalgo.com/validate/abc",
        "https://checkout-sbx.dlocalgo.com.evil.example/validate/abc",
        "https://user@checkout-sbx.dlocalgo.com/validate/abc",
        "https://checkout-sbx.dlocalgo.com/validate/abc?redirect=evil",
    ],
)
def test_checkout_destinations_cannot_escape_provider(url: str) -> None:
    credentials = Credentials("sandbox", KEY, SECRET)
    assert KEY not in repr(credentials) and SECRET not in repr(credentials)
    with pytest.raises(GatewayError):
        credentials.checkout_url(url)


@pytest.mark.parametrize(
    "field,value",
    [
        ("amount", True),
        ("amount", 1.001),
        ("currency", "EUR"),
        ("order_id", "intruder"),
        ("id", "DP-other"),
        ("status", "APPROVED"),
    ],
)
def test_receipt_identity_and_exact_money(field: str, value: Any) -> None:
    data = {
        "id": "DP-synthetic",
        "order_id": "reference",
        "country": "EC",
        "amount": 10,
        "currency": "USD",
        "status": "PAID",
        field: value,
    }
    with pytest.raises(GatewayError):
        validate_payment(data, reference="reference", cents=1000, payment_id="DP-synthetic")


@pytest.fixture
def web(settings: Settings, catalog: Catalog) -> Iterator[tuple[TestClient, Provider]]:
    settings.billing_enabled = True
    settings.billing_platform_tenant = "globex"
    settings.public_base_url = "https://synthetic.example"
    orch = Orchestrator(settings, catalog=catalog, llm=FakeLLM())
    provider = Provider()
    orch.dlocal_billing.gateway = DlocalGo(httpx.MockTransport(provider.handle))
    with TestClient(create_app(settings, orch)) as client:
        yield client, provider


def test_http_private_connection_permissions_and_no_false_return_settlement(web: Any) -> None:
    client, provider = web
    body = {"api_key": KEY, "secret_key": SECRET, "environment": "sandbox"}
    assert client.put("/v1/billing/dlocal/account", json=body).status_code == 401
    assert (
        client.put("/v1/billing/platform/dlocal/account", json=body, headers=ACME).status_code
        == 403
    )
    connected = client.put("/v1/billing/dlocal/account", json=body, headers=ACME)
    assert connected.status_code == 200
    assert KEY not in connected.text and SECRET not in connected.text
    returned = client.get("/v1/billing/dlocal/return?status=PAID&amount=1")
    assert returned.status_code == 200 and returned.headers["cache-control"] == "no-store"
    assert all(r.method == "GET" for r in provider.requests)
    assert (
        client.post(
            "/v1/billing/dlocal/subscription", headers=ACME, json={"consent": False}
        ).status_code
        == 409
    )
    hook = client.post(
        "/v1/billing/dlocal/hooks/" + "0" * 32, content=b'{"payment_id":"DP-synthetic"}'
    )
    assert hook.status_code == 404


def test_http_subscription_receiver_stays_with_operator(web: Any) -> None:
    client, _ = web
    response = client.put(
        "/v1/billing/platform/dlocal/account",
        headers=GLOBEX,
        json={"api_key": PLATFORM_KEY, "secret_key": PLATFORM_SECRET},
    )
    assert response.status_code == 200
    started = client.post("/v1/billing/dlocal/subscription", headers=ACME, json={"consent": True})
    assert started.status_code == 200 and started.json()["state"] == "awaiting"
    assert (
        client.get("/v1/billing/dlocal/subscription", headers=GLOBEX).json()["state"]
        == "not_started"
    )
