"""Payment reconciliation and capped, opt-in WhatsApp utility reminders.

The worker only reads the gateway; it never creates charges. An accepted message
is not delivery or payment. A crashed/uncertain send is never retried blindly.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Integer,
    String,
    Table,
    UniqueConstraint,
    and_,
    insert,
    select,
    update,
)
from sqlalchemy.exc import IntegrityError

from orchestrator.billing import BillingError
from orchestrator.billing_followup import contacts, events, followups, phone_number
from orchestrator.billing_store import orders
from orchestrator.db import aware, metadata, utcnow
from orchestrator.dlocal_billing import accounts, checkouts, plans, renewals
from orchestrator.inbound import address_key
from orchestrator.publishers import WhatsAppSender
from orchestrator.social import channel_accounts
from orchestrator.social_credentials import ReconnectRequired, SocialCredentials

log = logging.getLogger(__name__)

policies = Table(
    "billing_auto_policies",
    metadata,
    Column("tenant", String(64), primary_key=True),
    Column("scope", String(16), primary_key=True),
    Column("enabled", Boolean, nullable=False),
    Column("account_id", String(32), nullable=True),
    Column("template_name", String(128), nullable=False),
    Column("language", String(12), nullable=False),
    Column("cadence_days", Integer, nullable=False),
    Column("maximum", Integer, nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)
targets = Table(
    "billing_auto_targets",
    metadata,
    Column("tenant", String(64), primary_key=True),
    Column("source_key", String(160), primary_key=True),
    Column("kind", String(16), nullable=False),
    Column("source_id", String(128), nullable=False),
    Column("enabled", Boolean, nullable=False),
    Column("due_at", DateTime(timezone=True), nullable=False),
    Column("next_at", DateTime(timezone=True), nullable=False),
    Column("state", String(32), nullable=False),
    Column("last_result", String(48), nullable=True),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)
attempts = Table(
    "billing_auto_attempts",
    metadata,
    Column("tenant", String(64), primary_key=True),
    Column("attempt_id", String(32), primary_key=True),
    Column("source_key", String(160), nullable=False),
    Column("ordinal", Integer, nullable=False),
    Column("state", String(32), nullable=False),
    Column("provider_message_id", String(256), nullable=True),
    Column("simulated", Boolean, nullable=False),
    Column("sender_tenant", String(64), nullable=False),
    Column("contact_day", String(10), nullable=False),
    Column("address_key", String(64), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("tenant", "source_key", "ordinal", name="uq_billing_auto_attempt"),
    UniqueConstraint("sender_tenant", "address_key", "contact_day", name="uq_billing_auto_daily"),
)


class BillingAutomation:
    def __init__(self, orchestrator: Any, transport: httpx.AsyncBaseTransport | None = None):
        self.o, self.db = orchestrator, orchestrator.db
        self.transport = transport

    def active(self) -> bool:
        return bool(self.o.settings.billing_enabled and self.o.settings.billing_automation_enabled)

    async def policy(self, tenant: str, platform: bool = False) -> dict[str, Any]:
        scope = "platform" if platform else "clinic"
        async with self.db.engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        select(policies).where(
                            and_(
                                policies.c.tenant == tenant,
                                policies.c.scope == scope,
                            )
                        )
                    )
                )
                .mappings()
                .first()
            )
            available = (
                (
                    await conn.execute(
                        select(
                            channel_accounts.c.account_id,
                            channel_accounts.c.handle,
                        ).where(
                            and_(
                                channel_accounts.c.tenant == tenant,
                                channel_accounts.c.network == "whatsapp",
                                channel_accounts.c.active.is_(True),
                            )
                        )
                    )
                )
                .mappings()
                .all()
            )
            query = (
                select(targets).where(targets.c.kind == "renewal")
                if platform
                else select(targets).where(
                    and_(targets.c.tenant == tenant, targets.c.kind == "order")
                )
            )
            work = (
                (await conn.execute(query.order_by(targets.c.next_at).limit(100))).mappings().all()
            )
        return {
            "enabled": bool(row and row["enabled"]),
            "account_id": row["account_id"] if row else None,
            "template_name": row["template_name"] if row else "",
            "language": row["language"] if row else "es",
            "cadence_days": row["cadence_days"] if row else 1,
            "maximum": row["maximum"] if row else 3,
            "server_enabled": self.active(),
            "accounts": [dict(a) for a in available],
            "targets": [self.public_target(dict(t)) for t in work],
            "hours": "09:00-18:00",
            "timezone": self.o.settings.clinic_timezone,
        }

    async def configure_policy(
        self,
        tenant: str,
        actor: str,
        *,
        platform: bool = False,
        **values: Any,
    ) -> dict[str, Any]:
        scope = "platform" if platform else "clinic"
        if values["enabled"]:
            if not values["account_id"] or not values["template_name"]:
                raise BillingError("WhatsApp account and approved utility template are required")
            await self.sender(tenant, values["account_id"])
        key = and_(policies.c.tenant == tenant, policies.c.scope == scope)
        async with self.db.engine.begin() as conn:
            values["updated_at"] = utcnow()
            if (await conn.execute(update(policies).where(key).values(**values))).rowcount == 0:
                await conn.execute(insert(policies).values(tenant=tenant, scope=scope, **values))
            await self.o.audit.record_in(
                conn, tenant, actor, "billing.automation.configured", f"billing/automation/{scope}"
            )
        return await self.policy(tenant, platform)

    @staticmethod
    def public_target(row: dict[str, Any]) -> dict[str, Any]:
        return {
            **{
                k: row[k]
                for k in (
                    "tenant",
                    "source_key",
                    "kind",
                    "source_id",
                    "enabled",
                    "state",
                    "last_result",
                )
            },
            "due_at": aware(row["due_at"]),
            "next_at": aware(row["next_at"]),
        }

    async def target(self, tenant: str, key: str) -> dict[str, Any] | None:
        async with self.db.engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        select(targets).where(
                            and_(targets.c.tenant == tenant, targets.c.source_key == key)
                        )
                    )
                )
                .mappings()
                .first()
            )
        return self.public_target(dict(row)) if row else None

    async def configure_order(
        self, tenant: str, invoice_id: str, enabled: bool, due_at: datetime | None, actor: str
    ) -> dict[str, Any]:
        async with self.db.engine.connect() as conn:
            order = await self.o.payment_followups._owned(conn, tenant, invoice_id, False)
            checkout = (
                await conn.execute(
                    select(checkouts.c.payment_id).where(
                        and_(
                            checkouts.c.tenant == tenant,
                            checkouts.c.invoice_id == invoice_id,
                        )
                    )
                )
            ).scalar_one_or_none()
            draft = (
                await conn.execute(
                    select(events.c.event_id).where(
                        and_(
                            events.c.tenant == tenant,
                            events.c.invoice_id == invoice_id,
                            events.c.open_slot == "open",
                        )
                    )
                )
            ).first()
        if enabled and (order["method"] != "card" or not checkout):
            raise BillingError(
                "automatic verification requires a payment registered with the gateway"
            )
        if enabled and order["state"] in ("paid", "cancelled"):
            raise BillingError("this payment does not need automatic reminders")
        if enabled and draft:
            raise BillingError("discard the manual draft before enabling automatic reminders")
        return await self.configure_target(
            tenant, "order:" + invoice_id, "order", invoice_id, enabled, due_at, actor
        )

    async def configure_target(
        self,
        tenant: str,
        key: str,
        kind: str,
        source_id: str,
        enabled: bool,
        due_at: datetime | None,
        actor: str,
    ) -> dict[str, Any]:
        if due_at is not None and due_at.tzinfo is None:
            raise BillingError("due date requires a timezone")
        where = and_(targets.c.tenant == tenant, targets.c.source_key == key)
        async with self.db.engine.begin() as conn:
            old = (
                (await conn.execute(select(targets).where(where).with_for_update()))
                .mappings()
                .first()
            )
            unresolved = (
                await conn.execute(
                    select(attempts.c.attempt_id).where(
                        and_(
                            attempts.c.tenant == tenant,
                            attempts.c.source_key == key,
                            attempts.c.state.in_(["sending", "uncertain"]),
                        )
                    )
                )
            ).first()
            if enabled and unresolved:
                raise BillingError(
                    "automatic send requires delivery reconciliation before resuming"
                )
            if enabled and old and old["state"] in ("sending", "uncertain", "failed"):
                raise BillingError(
                    "automatic send requires delivery reconciliation before resuming"
                )
            due = aware(due_at) or (aware(old["due_at"]) if old else None)
            if due is None:
                raise BillingError("choose the payment due date before scheduling reminders")
            first_contact = (
                due.astimezone(ZoneInfo(self.o.settings.clinic_timezone)) + timedelta(days=1)
            ).replace(hour=9, minute=0, second=0, microsecond=0)
            first_contact = first_contact.astimezone(UTC)
            values = dict(
                enabled=enabled,
                due_at=due,
                next_at=max(first_contact, aware(old["next_at"]) or first_contact)
                if old
                else first_contact,
                state=(
                    old["state"]
                    if old and old["state"] in ("sending", "uncertain")
                    else "scheduled"
                    if enabled
                    else "paused"
                ),
                updated_at=utcnow(),
            )
            if old:
                await conn.execute(update(targets).where(where).values(**values))
            else:
                await conn.execute(
                    insert(targets).values(
                        tenant=tenant, source_key=key, kind=kind, source_id=source_id, **values
                    )
                )
            await self.o.audit.record_in(
                conn, tenant, actor, "billing.automation.scheduled", f"billing/{key}"
            )
        result = await self.target(tenant, key)
        assert result is not None
        return result

    async def sender(self, tenant: str, account_id: str) -> tuple[Any, str]:
        async with self.db.engine.connect() as conn:
            account = (
                (
                    await conn.execute(
                        select(channel_accounts).where(
                            and_(
                                channel_accounts.c.tenant == tenant,
                                channel_accounts.c.account_id == account_id,
                                channel_accounts.c.network == "whatsapp",
                                channel_accounts.c.active.is_(True),
                            )
                        )
                    )
                )
                .mappings()
                .first()
            )
        if not account:
            raise BillingError("connect the sender's WhatsApp account")
        try:
            token = await SocialCredentials(self.db, self.o.settings).resolve(
                tenant,
                account["secret_ref"],
                "whatsapp",
                account["external_id"],
            )
        except ReconnectRequired:
            raise BillingError("WhatsApp authorization needs reconnection") from None
        if not token:
            raise BillingError("WhatsApp authorization needs reconnection")
        return account, token

    async def reconcile_due(self, now: datetime) -> int:
        count = 0
        async with self.db.engine.connect() as conn:
            payments = (
                await conn.execute(
                    select(checkouts.c.tenant, checkouts.c.invoice_id)
                    .join(
                        orders,
                        and_(
                            orders.c.tenant == checkouts.c.tenant,
                            orders.c.invoice_id == checkouts.c.invoice_id,
                        ),
                    )
                    .where(
                        and_(
                            checkouts.c.payment_id.is_not(None),
                            checkouts.c.updated_at < now - timedelta(minutes=5),
                            orders.c.state.in_(["processing", "uncertain"]),
                        )
                    )
                    .order_by(checkouts.c.updated_at)
                    .limit(20)
                )
            ).all()
            subscriptions = (
                (
                    await conn.execute(
                        select(plans.c.tenant)
                        .where(
                            and_(
                                plans.c.plan_id.is_not(None),
                                plans.c.state != "cancelled",
                                plans.c.updated_at < now - timedelta(minutes=5),
                            )
                        )
                        .order_by(plans.c.updated_at)
                        .limit(20)
                    )
                )
                .scalars()
                .all()
            )
        for tenant, invoice_id in payments:
            async with self.db.engine.begin() as conn:
                claimed = await conn.execute(
                    update(checkouts)
                    .where(
                        and_(
                            checkouts.c.tenant == tenant,
                            checkouts.c.invoice_id == invoice_id,
                            checkouts.c.updated_at < now - timedelta(minutes=5),
                        )
                    )
                    .values(updated_at=now)
                )
            if claimed.rowcount:
                try:
                    await self.o.dlocal_billing.reconcile_payment(tenant, invoice_id)
                    count += 1
                except Exception:
                    log.warning("Automatic payment reconciliation unavailable; no payment inferred")
        for tenant in subscriptions:
            async with self.db.engine.begin() as conn:
                claimed = await conn.execute(
                    update(plans)
                    .where(
                        and_(
                            plans.c.tenant == tenant,
                            plans.c.updated_at < now - timedelta(minutes=5),
                        )
                    )
                    .values(updated_at=now)
                )
            if claimed.rowcount:
                try:
                    await self.o.dlocal_billing.sync_subscription(tenant)
                    count += 1
                except Exception:
                    log.warning(
                        "Automatic subscription reconciliation unavailable; no payment inferred"
                    )
        return count

    async def latest_renewal(self, tenant: str) -> Any:
        async with self.db.engine.connect() as conn:
            rows = (
                (await conn.execute(select(renewals).where(renewals.c.tenant == tenant)))
                .mappings()
                .all()
            )
        if not rows or any(not r["provider_created_at"] for r in rows):
            return None  # Incomplete chronology cannot establish the latest payment attempt.
        parsed = [
            (datetime.fromisoformat(r["provider_created_at"].replace("Z", "+00:00")), r)
            for r in rows
        ]
        if len({d.tzinfo is None for d, _ in parsed}) != 1:
            return None
        parsed.sort(key=lambda pair: pair[0])
        if len(parsed) > 1 and parsed[-1][0] == parsed[-2][0]:
            return None
        return parsed[-1][1]

    async def enroll_renewals(self, now: datetime) -> None:
        owner = self.o.settings.billing_platform_tenant
        policy = await self.policy(owner, True)
        if not policy["enabled"]:
            return
        async with self.db.engine.connect() as conn:
            tenants = (
                (await conn.execute(select(plans.c.tenant).where(plans.c.state == "active")))
                .scalars()
                .all()
            )
        for tenant in tenants:
            latest = await self.latest_renewal(tenant)
            if latest and latest["state"] == "rejected":
                key = "renewal:" + latest["execution_id"]
                if await self.target(tenant, key) is None:
                    await self.configure_target(
                        tenant,
                        key,
                        "renewal",
                        latest["execution_id"],
                        True,
                        now,
                        "system:billing-auto",
                    )

    async def source(
        self, target: dict[str, Any], now: datetime
    ) -> tuple[dict[str, Any], str, bool]:
        tenant = target["tenant"]
        if target["kind"] == "order":
            context = await self.o.payment_followups._context(tenant, target["source_id"], False)
            if context["order"]["method"] != "card":
                raise BillingError("payment_needs_review")
            result = await self.o.dlocal_billing.reconcile_payment(tenant, target["source_id"])
            context = await self.o.payment_followups._context(tenant, target["source_id"], False)
            if result["state"] == "paid":
                raise BillingError("paid")
            if not (
                result["provider_status"] == "REJECTED"
                or (
                    result["provider_status"] == "PENDING"
                    and result["provider_status_detail"] == "PENDING_PAYMENT"
                )
            ):
                raise BillingError("payment_needs_review")
            async with self.db.engine.connect() as conn:
                account = (
                    (
                        await conn.execute(
                            select(accounts.c.environment, accounts.c.withdrawals_confirmed).where(
                                and_(
                                    accounts.c.tenant == tenant,
                                    accounts.c.scope == "clinic",
                                )
                            )
                        )
                    )
                    .mappings()
                    .one()
                )
            context["order"] = {**context["order"], "state": "pending"}
        else:
            await self.o.dlocal_billing.sync_subscription(tenant)
            latest = await self.latest_renewal(tenant)
            if (
                not latest
                or latest["execution_id"] != target["source_id"]
                or latest["state"] != "rejected"
            ):
                raise BillingError("renewal_no_longer_pending")
            async with self.db.engine.connect() as conn:
                plan = (
                    (await conn.execute(select(plans).where(plans.c.tenant == tenant)))
                    .mappings()
                    .one()
                )
                person = (
                    (await conn.execute(select(contacts).where(contacts.c.tenant == tenant)))
                    .mappings()
                    .first()
                )
                account = (
                    (
                        await conn.execute(
                            select(accounts.c.environment, accounts.c.withdrawals_confirmed).where(
                                and_(
                                    accounts.c.tenant == self.o.settings.billing_platform_tenant,
                                    accounts.c.scope == "platform",
                                )
                            )
                        )
                    )
                    .mappings()
                    .one()
                )
            if plan["state"] != "active":
                raise BillingError("subscription_not_active")
            phone = (
                phone_number(self.o.payment_followups.sealer.open(tenant, person["phone_sealed"]))
                if person
                else None
            )
            sender = self.o.settings.billing_platform_tenant
            context = {
                "order": {
                    "state": "pending",
                    "amount_cents": plan["amount_cents"],
                    "patient_id": None,
                },
                "follow": {},
                "allowed": bool(person and person["whatsapp_allowed"]),
                "active": True,
                "phone": phone,
                "opted_out": bool(
                    phone and await self.o.inbound.is_opted_out(sender, "whatsapp", phone)
                ),
                "sender": sender,
            }
        reason = self.o.payment_followups._reason(context, now)
        if reason:
            raise BillingError(reason)
        simulated = account["environment"] == "sandbox"
        if not simulated and (
            not self.o.settings.billing_dlocal_live_enabled or not account["withdrawals_confirmed"]
        ):
            raise BillingError("live_payments_not_enabled")
        if simulated and not (self.o.settings.app_env == "test" and self.transport is not None):
            raise BillingError("sandbox_delivery_disabled")
        return context, context["sender"], simulated

    async def due(self, now: datetime | None = None) -> dict[str, int]:
        result = {"reconciled": 0, "accepted": 0, "simulated": 0, "blocked": 0}
        if not self.active():
            return result
        now = aware(now) or utcnow()
        result["reconciled"] = await self.reconcile_due(now)
        await self.close_settled(now)
        await self.enroll_renewals(now)
        if not 9 <= now.astimezone(ZoneInfo(self.o.settings.clinic_timezone)).hour < 18:
            return result
        async with self.db.engine.begin() as conn:
            # A restart must not turn an in-flight HTTP request into another send.
            await conn.execute(
                update(targets)
                .where(
                    and_(
                        targets.c.state == "sending",
                        targets.c.updated_at < now - timedelta(minutes=10),
                    )
                )
                .values(state="uncertain", last_result="delivery_uncertain")
            )
            rows = (
                (
                    await conn.execute(
                        select(targets)
                        .where(
                            and_(
                                targets.c.enabled.is_(True),
                                targets.c.state == "scheduled",
                                targets.c.next_at <= now,
                            )
                        )
                        .order_by(targets.c.next_at)
                        .limit(20)
                    )
                )
                .mappings()
                .all()
            )
        for row in rows:
            target = dict(row)
            key = and_(targets.c.tenant == row["tenant"], targets.c.source_key == row["source_key"])
            try:
                context, sender, simulated = await self.source(target, now)
                policy = await self.policy(sender, row["kind"] == "renewal")
                if not policy["enabled"]:
                    raise BillingError("policy_disabled")
                account, token = await self.sender(sender, policy["account_id"])
                async with self.db.engine.begin() as conn:
                    current = (
                        (await conn.execute(select(targets).where(key).with_for_update()))
                        .mappings()
                        .one()
                    )
                    if (
                        not current["enabled"]
                        or current["state"] != "scheduled"
                        or (aware(current["next_at"]) or now) > now
                    ):
                        continue
                    previous = (
                        (
                            await conn.execute(
                                select(attempts.c.ordinal).where(
                                    and_(
                                        attempts.c.tenant == row["tenant"],
                                        attempts.c.source_key == row["source_key"],
                                    )
                                )
                            )
                        )
                        .scalars()
                        .all()
                    )
                    if len(previous) >= policy["maximum"]:
                        await conn.execute(update(targets).where(key).values(state="limit_reached"))
                        continue
                    fingerprint = address_key(
                        self.o.settings.pseudonym_secret(), sender, "whatsapp", context["phone"]
                    )
                    contact_day = (
                        now.astimezone(ZoneInfo(self.o.settings.clinic_timezone)).date().isoformat()
                    )
                    daily = (
                        await conn.execute(
                            select(attempts.c.attempt_id).where(
                                and_(
                                    attempts.c.sender_tenant == sender,
                                    attempts.c.address_key == fingerprint,
                                    attempts.c.contact_day == contact_day,
                                )
                            )
                        )
                    ).first()
                    if daily:
                        raise BillingError("daily_contact_limit")
                    if row["kind"] == "order":
                        payment_state = (
                            await conn.execute(
                                select(orders.c.state)
                                .where(
                                    and_(
                                        orders.c.tenant == row["tenant"],
                                        orders.c.invoice_id == row["source_id"],
                                    )
                                )
                                .with_for_update()
                            )
                        ).scalar_one()
                        if payment_state not in ("pending", "processing", "rejected"):
                            raise BillingError("payment_needs_review")
                        draft = (
                            await conn.execute(
                                select(events.c.event_id).where(
                                    and_(
                                        events.c.tenant == row["tenant"],
                                        events.c.invoice_id == row["source_id"],
                                        events.c.open_slot == "open",
                                    )
                                )
                            )
                        ).first()
                        if draft:
                            raise BillingError("manual_draft_open")
                    claimed = await conn.execute(
                        update(targets)
                        .where(
                            and_(
                                key,
                                targets.c.enabled.is_(True),
                                targets.c.state == "scheduled",
                                targets.c.next_at <= now,
                            )
                        )
                        .values(state="sending", updated_at=now)
                    )
                    if claimed.rowcount != 1:
                        continue
                    attempt_id = uuid.uuid4().hex
                    await conn.execute(
                        insert(attempts).values(
                            tenant=row["tenant"],
                            attempt_id=attempt_id,
                            source_key=row["source_key"],
                            ordinal=max(previous, default=0) + 1,
                            state="sending",
                            simulated=simulated,
                            sender_tenant=sender,
                            contact_day=contact_day,
                            address_key=address_key(
                                self.o.settings.pseudonym_secret(),
                                sender,
                                "whatsapp",
                                context["phone"],
                            ),
                            created_at=now,
                            updated_at=now,
                        )
                    )
                amount = context["order"]["amount_cents"]
                try:
                    async with httpx.AsyncClient(
                        timeout=self.o.settings.publish_timeout_seconds, transport=self.transport
                    ) as client:
                        message_id = await WhatsAppSender(
                            client, self.o.settings.whatsapp_graph_base
                        ).send_template(
                            phone_number_id=account["external_id"],
                            token=token,
                            to=context["phone"],
                            name=policy["template_name"],
                            language=policy["language"],
                            parameters=[
                                f"USD {amount // 100}.{amount % 100:02d}",
                                row["source_id"][:8],
                            ],
                        )
                    outcome = "simulated" if simulated else "accepted"
                    result[outcome] += 1
                except Exception:
                    outcome, message_id = "uncertain", None
                async with self.db.engine.begin() as conn:
                    await conn.execute(
                        update(attempts)
                        .where(
                            and_(
                                attempts.c.tenant == row["tenant"],
                                attempts.c.attempt_id == attempt_id,
                            )
                        )
                        .values(state=outcome, provider_message_id=message_id, updated_at=utcnow())
                    )
                    await conn.execute(
                        update(targets)
                        .where(key)
                        .values(
                            state="uncertain" if outcome == "uncertain" else "scheduled",
                            last_result=outcome,
                            next_at=now + timedelta(days=policy["cadence_days"]),
                            updated_at=utcnow(),
                        )
                    )
                    if row["kind"] == "order":
                        await conn.execute(
                            insert(events).values(
                                tenant=row["tenant"],
                                event_id=attempt_id,
                                invoice_id=row["source_id"],
                                state="auto_" + outcome,
                                open_slot=None,
                                address_key=address_key(
                                    self.o.settings.pseudonym_secret(),
                                    sender,
                                    "whatsapp",
                                    context["phone"],
                                ),
                                actor="system:billing-auto",
                                created_at=now,
                                confirmed_at=utcnow(),
                            )
                        )
                        if outcome in ("accepted", "simulated"):
                            follow_key = and_(
                                followups.c.tenant == row["tenant"],
                                followups.c.invoice_id == row["source_id"],
                            )
                            previous_next = (
                                await conn.execute(
                                    select(followups.c.next_contact_at)
                                    .where(follow_key)
                                    .with_for_update()
                                )
                            ).scalar_one_or_none()
                            values = dict(
                                next_contact_at=max(
                                    now + timedelta(days=policy["cadence_days"]),
                                    aware(previous_next) or now,
                                ),
                                updated_at=utcnow(),
                            )
                            if (
                                await conn.execute(
                                    update(followups).where(follow_key).values(**values)
                                )
                            ).rowcount == 0:
                                await conn.execute(
                                    insert(followups).values(
                                        tenant=row["tenant"],
                                        invoice_id=row["source_id"],
                                        paused=False,
                                        **values,
                                    )
                                )
                    await self.o.audit.record_in(
                        conn,
                        row["tenant"],
                        "system:billing-auto",
                        "billing.whatsapp.auto_" + outcome,
                        "billing/" + row["source_key"],
                        subject_id=context["order"]["patient_id"],
                    )
            except Exception as error:
                reason = (
                    str(error)
                    if isinstance(error, BillingError)
                    else "claim_conflict"
                    if isinstance(error, IntegrityError)
                    else "reconciliation_unavailable"
                )
                if reason not in {
                    "paid",
                    "renewal_no_longer_pending",
                    "payment_needs_review",
                    "paused",
                    "permission_missing",
                    "opted_out",
                    "phone_missing",
                    "inactive_record",
                    "payment_promised",
                    "contact_not_due",
                    "policy_disabled",
                    "sandbox_delivery_disabled",
                    "live_payments_not_enabled",
                    "subscription_not_active",
                    "manual_draft_open",
                    "daily_contact_limit",
                    "claim_conflict",
                }:
                    reason = "reconciliation_unavailable"
                async with self.db.engine.begin() as conn:
                    await conn.execute(
                        update(targets)
                        .where(and_(key, targets.c.state == "scheduled"))
                        .values(
                            enabled=False
                            if reason in ("paid", "renewal_no_longer_pending")
                            else row["enabled"],
                            last_result=reason,
                            state="settled"
                            if reason in ("paid", "renewal_no_longer_pending")
                            else "scheduled",
                            next_at=aware(
                                (
                                    now.astimezone(ZoneInfo(self.o.settings.clinic_timezone))
                                    + timedelta(days=1)
                                ).replace(hour=9, minute=0, second=0, microsecond=0)
                            )
                            if reason == "daily_contact_limit"
                            else now + timedelta(minutes=5),
                            updated_at=utcnow(),
                        )
                    )
                result["blocked"] += 1
        return result

    async def close_settled(self, now: datetime) -> None:
        now = aware(now) or utcnow()
        async with self.db.engine.begin() as conn:
            settled = (
                select(orders.c.invoice_id)
                .where(
                    and_(
                        orders.c.tenant == targets.c.tenant,
                        orders.c.invoice_id == targets.c.source_id,
                        orders.c.state == "paid",
                    )
                )
                .exists()
            )
            await conn.execute(
                update(targets)
                .where(and_(targets.c.kind == "order", targets.c.enabled.is_(True), settled))
                .values(enabled=False, state="settled", last_result="paid", updated_at=now)
            )
            rows = (
                (
                    await conn.execute(
                        select(targets)
                        .where(and_(targets.c.kind == "renewal", targets.c.enabled.is_(True)))
                        .limit(100)
                    )
                )
                .mappings()
                .all()
            )
        for row in rows:
            latest = await self.latest_renewal(row["tenant"])
            if latest and (latest["execution_id"] != row["source_id"] or latest["state"] == "paid"):
                async with self.db.engine.begin() as conn:
                    await conn.execute(
                        update(targets)
                        .where(
                            and_(
                                targets.c.tenant == row["tenant"],
                                targets.c.source_key == row["source_key"],
                            )
                        )
                        .values(
                            enabled=False,
                            state="settled",
                            last_result="renewal_no_longer_pending",
                            updated_at=now,
                        )
                    )
