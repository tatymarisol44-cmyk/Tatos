"""Durable dLocal Go accounts, hosted payments and provider-managed monthly plans.

An outbound creation is persisted BEFORE contacting the provider. An uncertain
creation is not repeated. Credentials and checkout URLs are sealed at rest.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    String,
    Table,
    Text,
    UniqueConstraint,
    and_,
    insert,
    select,
    update,
)
from sqlalchemy.exc import IntegrityError

from orchestrator.billing import BillingError, ChargeKind
from orchestrator.billing_store import BillingStore, orders, payees
from orchestrator.db import metadata, utcnow
from orchestrator.dlocal_go import (
    Credentials,
    DlocalGo,
    Environment,
    GatewayError,
    amount_cents,
    identifier,
    validate_payment,
    validate_plan,
)
from orchestrator.sealing import Sealer, SealError

accounts = Table(
    "billing_gateway_accounts",
    metadata,
    Column("tenant", String(64), primary_key=True),
    Column("scope", String(16), primary_key=True),
    Column("account_id", String(32), nullable=False, unique=True),
    Column("environment", String(16), nullable=False),
    Column("key_fingerprint", String(64), nullable=False, unique=True),
    Column("credentials_sealed", Text, nullable=False),
    Column("withdrawals_confirmed", Boolean, nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)
checkouts = Table(
    "billing_gateway_checkouts",
    metadata,
    Column("tenant", String(64), primary_key=True),
    Column("invoice_id", String(32), primary_key=True),
    Column("reference", String(32), nullable=False, unique=True),
    Column("account_id", String(32), nullable=False),
    Column("payment_id", String(128), nullable=True),
    Column("url_sealed", Text, nullable=True),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("account_id", "payment_id", name="uq_gateway_payment"),
)
plans = Table(
    "billing_gateway_plans",
    metadata,
    Column("tenant", String(64), primary_key=True),
    Column("reference", String(32), nullable=False, unique=True),
    Column("account_id", String(32), nullable=False),
    Column("plan_id", String(128), nullable=True),
    Column("amount_cents", BigInteger, nullable=False),
    Column("state", String(24), nullable=False),
    Column("url_sealed", Text, nullable=True),
    Column("consented_by", String(64), nullable=False),
    Column("consented_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("account_id", "plan_id", name="uq_gateway_clinic_plan"),
)
renewals = Table(
    "billing_gateway_renewals",
    metadata,
    Column("tenant", String(64), primary_key=True),
    Column("execution_id", String(128), primary_key=True),
    Column("subscription_id", String(128), nullable=False),
    Column("state", String(24), nullable=False),
    Column("amount_cents", BigInteger, nullable=True),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)


class DlocalBilling:
    def __init__(self, billing: BillingStore, gateway: DlocalGo | None = None) -> None:
        self.billing = billing
        self.db, self.audit, self.settings = billing.db, billing.audit, billing.settings
        self.gateway = gateway or DlocalGo()
        self.sealer = Sealer(self.settings.pseudonym_secret(), b"billing-dlocal-go-v1")

    def _origin(self) -> str:
        origin = self.settings.public_base_url.rstrip("/")
        parsed = urlsplit(origin)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.path
            or parsed.query
            or parsed.fragment
        ):
            raise BillingError("payment HTTPS origin is not configured")
        return origin

    def _credentials(self, row: Any, *, for_creation: bool = False) -> Credentials:
        if for_creation:
            self.billing._enabled()
            if row["environment"] == "live" and (
                not self.settings.billing_dlocal_live_enabled or not row["withdrawals_confirmed"]
            ):
                raise BillingError("live payments are not enabled for this account")
        try:
            data = json.loads(
                self.sealer.open(
                    f"{row['tenant']}/{row['scope']}/{row['account_id']}", row["credentials_sealed"]
                )
            )
        except (SealError, ValueError):
            raise BillingError("dLocal Go account needs reconnection") from None
        return Credentials(row["environment"], data["api_key"], data["secret_key"])

    async def _account(self, conn: Any, tenant: str, scope: str) -> Any:
        row = (
            (
                await conn.execute(
                    select(accounts).where(
                        and_(accounts.c.tenant == tenant, accounts.c.scope == scope)
                    )
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            raise BillingError("dLocal Go account is not configured")
        return row

    async def connect(
        self,
        tenant: str,
        scope: str,
        environment: Environment,
        api_key: str,
        secret_key: str,
        withdrawals_confirmed: bool,
        actor: str,
        *,
        operator: bool = False,
    ) -> dict[str, Any]:
        self.billing._scope(tenant, scope, operator)
        credentials = Credentials(environment, api_key, secret_key)
        if environment == "live" and not withdrawals_confirmed:
            raise BillingError("confirm account approval and withdrawals before live connection")
        await self.gateway.account(credentials)  # authentication only, never proof of KYC
        fingerprint = hashlib.sha256(f"{environment}:{api_key}".encode()).hexdigest()
        try:
            async with self.db.engine.begin() as conn:
                old = (
                    (
                        await conn.execute(
                            select(accounts)
                            .where(and_(accounts.c.tenant == tenant, accounts.c.scope == scope))
                            .with_for_update()
                        )
                    )
                    .mappings()
                    .first()
                )
                if old and old["key_fingerprint"] != fingerprint:
                    raise BillingError("reconcile existing account before replacing it")
                account_id = old["account_id"] if old else uuid.uuid4().hex
                values = dict(
                    environment=environment,
                    key_fingerprint=fingerprint,
                    credentials_sealed=self.sealer.seal(
                        f"{tenant}/{scope}/{account_id}",
                        json.dumps(
                            {
                                "api_key": api_key,
                                "secret_key": secret_key,
                            }
                        ),
                    ),
                    withdrawals_confirmed=withdrawals_confirmed,
                    updated_at=utcnow(),
                )
                if old:
                    await conn.execute(
                        update(accounts)
                        .where(and_(accounts.c.tenant == tenant, accounts.c.scope == scope))
                        .values(**values)
                    )
                else:
                    await conn.execute(
                        insert(accounts).values(
                            tenant=tenant, scope=scope, account_id=account_id, **values
                        )
                    )
                if await self.billing._payee(conn, tenant, scope) is None:
                    await conn.execute(
                        insert(payees).values(
                            tenant=tenant,
                            scope=scope,
                            merchant_id=uuid.uuid4().hex,
                            instructions="",
                            updated_at=utcnow(),
                        )
                    )
                await self.audit.record_in(
                    conn, tenant, actor, "billing.dlocal.connected", f"billing/{scope}"
                )
        except IntegrityError:
            raise BillingError("dLocal Go account already belongs to a payment recipient") from None
        return {"configured": True, "environment": environment, "scope": scope}

    async def configuration(self, tenant: str) -> dict[str, Any]:
        async with self.db.engine.connect() as conn:
            rows = (
                (
                    await conn.execute(
                        select(accounts).where(
                            accounts.c.tenant.in_([tenant, self.settings.billing_platform_tenant])
                        )
                    )
                )
                .mappings()
                .all()
            )
            own = next((r for r in rows if r["tenant"] == tenant and r["scope"] == "clinic"), None)
            platform = next(
                (
                    r
                    for r in rows
                    if r["scope"] == "platform"
                    and r["tenant"] == self.settings.billing_platform_tenant
                ),
                None,
            )

        def public(row: Any) -> Any:
            if row is None:
                return None
            return {
                "environment": row["environment"],
                "available": bool(
                    self.settings.billing_enabled
                    and (
                        row["environment"] == "sandbox"
                        or (
                            self.settings.billing_dlocal_live_enabled
                            and row["withdrawals_confirmed"]
                        )
                    )
                ),
            }

        return {"clinic": public(own), "platform": public(platform)}

    async def checkout(
        self,
        tenant: str,
        invoice_id: str,
        actor: str,
        *,
        patient_id: str | None = None,
    ) -> dict[str, Any]:
        self.billing._enabled()
        origin = self._origin()
        async with self.db.engine.begin() as conn:
            order = await self.billing._row(conn, tenant, invoice_id, patient_id)
            if order["kind"] != ChargeKind.APPOINTMENT:
                raise BillingError("use the monthly subscription checkout")
            account = await self._account(conn, tenant, "clinic")
            credentials = self._credentials(account, for_creation=True)
            existing = (
                (
                    await conn.execute(
                        select(checkouts).where(
                            and_(checkouts.c.tenant == tenant, checkouts.c.invoice_id == invoice_id)
                        )
                    )
                )
                .mappings()
                .first()
            )
            if existing:
                return self._checkout_public(existing, order)
            if order["state"] != "pending":
                raise BillingError("payment is already being reviewed or completed")
            claimed = await conn.execute(
                update(orders)
                .where(
                    and_(
                        orders.c.tenant == tenant,
                        orders.c.invoice_id == invoice_id,
                        orders.c.state == "pending",
                    )
                )
                .values(state="processing", method="card", updated_at=utcnow())
            )
            if claimed.rowcount != 1:
                raise BillingError("payment conflicted; reload its state")
            reference = uuid.uuid4().hex
            await conn.execute(
                insert(checkouts).values(
                    tenant=tenant,
                    invoice_id=invoice_id,
                    reference=reference,
                    account_id=account["account_id"],
                    updated_at=utcnow(),
                )
            )
            await self.audit.record_in(
                conn,
                tenant,
                actor,
                "billing.checkout.started",
                f"billing/{invoice_id}",
                subject_id=order["patient_id"],
            )
        # Never retry POST after a timeout: the provider may already have created it.
        try:
            response = await self.gateway.create_payment(
                credentials, reference, order["amount_cents"], origin
            )
            validate_payment(response, reference=reference, cents=order["amount_cents"])
            url = credentials.checkout_url(response.get("redirect_url"))
            payment_id = identifier(response.get("id"))
        except GatewayError:
            async with self.db.engine.begin() as conn:
                await conn.execute(
                    update(orders)
                    .where(
                        and_(
                            orders.c.tenant == tenant,
                            orders.c.invoice_id == invoice_id,
                            orders.c.state == "processing",
                        )
                    )
                    .values(state="uncertain", updated_at=utcnow())
                )
            raise
        async with self.db.engine.begin() as conn:
            await conn.execute(
                update(checkouts)
                .where(checkouts.c.reference == reference)
                .values(
                    payment_id=payment_id,
                    url_sealed=self.sealer.seal(reference, url),
                    updated_at=utcnow(),
                )
            )
        return {"checkout_url": url, "state": "processing", "environment": credentials.environment}

    def _checkout_public(self, row: Any, order: Any) -> dict[str, Any]:
        return {
            "state": order["state"],
            "checkout_url": self.sealer.open(row["reference"], row["url_sealed"])
            if row["url_sealed"] and order["state"] == "processing"
            else None,
        }

    async def reconcile_payment(
        self,
        tenant: str,
        invoice_id: str,
        *,
        patient_id: str | None = None,
        notified_id: str | None = None,
    ) -> dict[str, Any]:
        async with self.db.engine.connect() as conn:
            order = await self.billing._row(conn, tenant, invoice_id, patient_id)
            row = (
                (
                    await conn.execute(
                        select(checkouts).where(
                            and_(checkouts.c.tenant == tenant, checkouts.c.invoice_id == invoice_id)
                        )
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                raise KeyError("payment not found")
            account = await self._account(conn, tenant, "clinic")
        if account["account_id"] != row["account_id"]:
            raise BillingError("payment account binding does not match")
        payment_id = row["payment_id"] or notified_id
        if payment_id is None:
            raise BillingError("provider payment is uncertain; recipient must reconcile it")
        credentials = self._credentials(account)
        response = await self.gateway.payment(credentials, payment_id)
        state = validate_payment(
            response, reference=row["reference"], cents=order["amount_cents"], payment_id=payment_id
        )
        async with self.db.engine.begin() as conn:
            current = await self.billing._row(conn, tenant, invoice_id, patient_id)
            if current["state"] != "paid":
                await conn.execute(
                    update(orders)
                    .where(
                        and_(
                            orders.c.tenant == tenant,
                            orders.c.invoice_id == invoice_id,
                            orders.c.state != "paid",
                        )
                    )
                    .values(
                        state=state,
                        settlement_id=f"dlocal:{payment_id}" if state == "paid" else None,
                        updated_at=utcnow(),
                    )
                )
                if state != current["state"]:
                    await self.audit.record_in(
                        conn,
                        tenant,
                        "dlocal-go",
                        "billing.checkout.verified",
                        f"billing/{invoice_id}",
                        subject_id=order["patient_id"],
                    )
            await conn.execute(
                update(checkouts)
                .where(checkouts.c.reference == row["reference"])
                .values(payment_id=payment_id, updated_at=utcnow())
            )
            return self.billing._public(
                await self.billing._row(conn, tenant, invoice_id, patient_id)
            )

    async def subscribe(self, tenant: str, actor: str, consent: bool) -> dict[str, Any]:
        self.billing._enabled()
        if not consent:
            raise BillingError("monthly renewal consent is required")
        origin = self._origin()
        async with self.db.engine.begin() as conn:
            account = await self._account(conn, self.settings.billing_platform_tenant, "platform")
            credentials = self._credentials(account, for_creation=True)
            await conn.execute(
                select(payees.c.merchant_id)
                .where(
                    and_(
                        payees.c.tenant == self.settings.billing_platform_tenant,
                        payees.c.scope == "platform",
                    )
                )
                .with_for_update()
            )
            existing = await conn.execute(select(plans).where(plans.c.tenant == tenant))
            row = existing.mappings().first()
            if row:
                return self._plan_public(row, include_url=row["state"] == "awaiting")
            # Do not start a card plan alongside a submitted or settled manual monthly invoice.
            transfer = (
                await conn.execute(
                    select(orders.c.invoice_id).where(
                        and_(
                            orders.c.tenant == tenant,
                            orders.c.kind == "subscription",
                            orders.c.resource_id == utcnow().strftime("%Y-%m"),
                            orders.c.state.in_(["transfer_review", "paid"]),
                        )
                    )
                )
            ).first()
            if transfer:
                raise BillingError("this month already has a transfer or payment under review")
            reference = uuid.uuid4().hex
            cents = self.settings.billing_monthly_price_cents
            if cents <= 0:
                raise BillingError("monthly price is not configured")
            await conn.execute(
                insert(plans).values(
                    tenant=tenant,
                    reference=reference,
                    account_id=account["account_id"],
                    amount_cents=cents,
                    state="preparing",
                    consented_by=actor,
                    consented_at=utcnow(),
                    updated_at=utcnow(),
                )
            )
            await self.audit.record_in(
                conn, tenant, actor, "billing.subscription.consent", "billing/subscription"
            )
        try:
            data = await self.gateway.create_plan(credentials, reference, cents, origin)
            validate_plan(data, cents)
            if data.get("active") is not True:
                raise GatewayError("monthly plan is not available")
            url = credentials.checkout_url(data.get("subscribe_url"), subscription=True)
            plan_id = identifier(data.get("id"))
        except GatewayError:
            async with self.db.engine.begin() as conn:
                await conn.execute(
                    update(plans)
                    .where(plans.c.tenant == tenant)
                    .values(state="uncertain", updated_at=utcnow())
                )
            raise
        async with self.db.engine.begin() as conn:
            await conn.execute(
                update(plans)
                .where(plans.c.tenant == tenant)
                .values(
                    plan_id=plan_id,
                    state="awaiting",
                    url_sealed=self.sealer.seal(reference, url),
                    updated_at=utcnow(),
                )
            )
        return {
            "state": "awaiting",
            "checkout_url": url,
            "amount_cents": cents,
            "environment": credentials.environment,
        }

    def _plan_public(self, row: Any, *, include_url: bool = False) -> dict[str, Any]:
        return {
            "state": row["state"],
            "amount_cents": row["amount_cents"],
            "checkout_url": self.sealer.open(row["reference"], row["url_sealed"])
            if include_url and row["url_sealed"]
            else None,
        }

    async def subscription(self, tenant: str) -> dict[str, Any]:
        async with self.db.engine.connect() as conn:
            row = (
                (await conn.execute(select(plans).where(plans.c.tenant == tenant)))
                .mappings()
                .first()
            )
            history = await conn.execute(
                select(renewals)
                .where(renewals.c.tenant == tenant)
                .order_by(renewals.c.updated_at.desc())
                .limit(100)
            )
            records = [dict(r) for r in history.mappings()]
        return {
            **(self._plan_public(row) if row else {"state": "not_started"}),
            "renewals": records,
        }

    async def sync_subscription(self, tenant: str, *, cancel: bool = False) -> dict[str, Any]:
        async with self.db.engine.connect() as conn:
            row = (
                (await conn.execute(select(plans).where(plans.c.tenant == tenant)))
                .mappings()
                .first()
            )
            if row is None or row["plan_id"] is None:
                raise BillingError("subscription is not ready for reconciliation")
            account = await self._account(conn, self.settings.billing_platform_tenant, "platform")
        if account["account_id"] != row["account_id"]:
            raise BillingError("subscription recipient does not match")
        credentials = self._credentials(account)
        plan = await self.gateway.plan(credentials, row["plan_id"])
        validate_plan(plan, row["amount_cents"], row["plan_id"])
        if cancel:
            # This plan is unique to one clinic; disabling it closes its unused link too.
            await self.gateway.cancel_plan(credentials, row["plan_id"])
            async with self.db.engine.begin() as conn:
                await conn.execute(
                    update(plans)
                    .where(plans.c.tenant == tenant)
                    .values(
                        state="cancelled",
                        updated_at=utcnow(),
                    )
                )
                await self.audit.record_in(
                    conn,
                    tenant,
                    "dlocal-go",
                    "billing.subscription.cancelled",
                    "billing/subscription",
                )
            return await self.subscription(tenant)
        subscribers = await self.gateway.subscribers(credentials, row["plan_id"])
        active = []
        for subscriber in subscribers:
            if str(subscriber.get("plan", {}).get("id")) != row["plan_id"]:
                raise GatewayError("subscription plan does not match")
            identifier(subscriber.get("id"))
            if subscriber.get("active") is True:
                active.append(subscriber)
        if len(active) > 1:
            raise GatewayError("multiple active mandates require recipient review")
        state = (
            "cancelled"
            if plan.get("active") is False
            else "active"
            if active and active[0].get("status") == "CONFIRMED"
            else (
                "cancelled"
                if not active and any(s.get("status") == "CONFIRMED" for s in subscribers)
                else "awaiting"
            )
        )
        records = []
        for subscriber in subscribers:
            sub_id = str(subscriber["id"])
            executions = await self.gateway.executions(credentials, row["plan_id"], sub_id)
            for execution in executions:
                sub = execution.get("subscription", {})
                if (
                    str(sub.get("id")) != sub_id
                    or str(sub.get("plan", {}).get("id")) != row["plan_id"]
                ):
                    raise GatewayError("subscription execution does not match")
                raw_state = execution.get("status")
                result = {"COMPLETED": "paid", "DECLINED": "rejected", "PENDING": "processing"}
                if raw_state not in result:
                    raise GatewayError("unknown renewal state")
                paid_cents = None
                if raw_state == "COMPLETED":
                    if (
                        execution.get("currency") != "USD"
                        or execution.get("checkout_currency") != "USD"
                    ):
                        raise GatewayError("renewal currency does not match")
                    paid_cents = amount_cents(execution.get("amount_paid"))
                    if paid_cents != row["amount_cents"]:
                        raise GatewayError("renewal amount does not match")
                records.append(
                    dict(
                        tenant=tenant,
                        execution_id=identifier(execution.get("id")),
                        subscription_id=sub_id,
                        state=result[raw_state],
                        amount_cents=paid_cents,
                        updated_at=utcnow(),
                    )
                )
        async with self.db.engine.begin() as conn:
            await conn.execute(
                update(plans)
                .where(plans.c.tenant == tenant)
                .values(state=state, updated_at=utcnow())
            )
            for values in records:
                exists = (
                    await conn.execute(
                        select(renewals.c.state).where(
                            and_(
                                renewals.c.tenant == tenant,
                                renewals.c.execution_id == values["execution_id"],
                            )
                        )
                    )
                ).first()
                if exists:
                    if exists[0] != "paid":
                        await conn.execute(
                            update(renewals)
                            .where(
                                and_(
                                    renewals.c.tenant == tenant,
                                    renewals.c.execution_id == values["execution_id"],
                                )
                            )
                            .values(**values)
                        )
                else:
                    await conn.execute(insert(renewals).values(**values))
            await self.audit.record_in(
                conn,
                tenant,
                "dlocal-go",
                "billing.subscription.cancelled" if cancel else "billing.subscription.verified",
                "billing/subscription",
            )
        return await self.subscription(tenant)

    async def webhook(self, reference: str, payload: bytes, authorization: str) -> None:
        async with self.db.engine.connect() as conn:
            checkout = (
                (await conn.execute(select(checkouts).where(checkouts.c.reference == reference)))
                .mappings()
                .first()
            )
            plan = (
                (await conn.execute(select(plans).where(plans.c.reference == reference)))
                .mappings()
                .first()
            )
            if checkout is None and plan is None:
                raise KeyError("payment notification not found")
            row = checkout if checkout is not None else plan
            assert row is not None
            account = (
                (
                    await conn.execute(
                        select(accounts).where(accounts.c.account_id == row["account_id"])
                    )
                )
                .mappings()
                .one()
            )
        credentials = self._credentials(account)
        if not credentials.valid_signature(payload, authorization):
            raise PermissionError("invalid dLocal Go signature")
        if checkout is not None:
            try:
                data = json.loads(payload)
                payment_id = identifier(data["payment_id"])
            except (KeyError, TypeError, ValueError):
                raise BillingError("invalid payment notification") from None
            if checkout["payment_id"] and checkout["payment_id"] != payment_id:
                raise BillingError("payment notification does not match")
            await self.reconcile_payment(
                checkout["tenant"], checkout["invoice_id"], notified_id=payment_id
            )
        else:
            await self.sync_subscription(row["tenant"])
