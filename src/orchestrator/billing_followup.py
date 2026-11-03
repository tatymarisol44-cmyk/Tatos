"""First-party invoice follow-up, with a user-reviewed WhatsApp composer.

Preparing a link is NOT sending a message. No background sender is registered.
Manual contact reports never settle an invoice or prove delivery/read receipts.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlencode

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    String,
    Table,
    Text,
    UniqueConstraint,
    and_,
    insert,
    or_,
    select,
    update,
)
from sqlalchemy.exc import IntegrityError

from orchestrator.billing import BillingError
from orchestrator.billing_store import orders
from orchestrator.crm import appointments, patients
from orchestrator.db import aware, metadata, utcnow
from orchestrator.governance import Purpose
from orchestrator.inbound import address_key
from orchestrator.sealing import Sealer

followups = Table(
    "billing_followups",
    metadata,
    Column("tenant", String(64), primary_key=True),
    Column("invoice_id", String(32), primary_key=True),
    Column("next_contact_at", DateTime(timezone=True), nullable=True),
    Column("promised_for", DateTime(timezone=True), nullable=True),
    Column("paused", Boolean, nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)
events = Table(
    "billing_followup_events",
    metadata,
    Column("tenant", String(64), primary_key=True),
    Column("event_id", String(32), primary_key=True),
    Column("invoice_id", String(32), nullable=False),
    Column("state", String(24), nullable=False),
    Column("open_slot", String(8), nullable=True),
    Column("address_key", String(64), nullable=False),
    Column("actor", String(128), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("confirmed_at", DateTime(timezone=True), nullable=True),
    UniqueConstraint("tenant", "invoice_id", "open_slot", name="uq_billing_followup_open"),
)
contacts = Table(
    "billing_subscription_contacts",
    metadata,
    Column("tenant", String(64), primary_key=True),
    Column("phone_sealed", Text, nullable=False),
    Column("whatsapp_allowed", Boolean, nullable=False),
    Column("consent_source", String(128), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)


def phone_number(value: str | None) -> str | None:
    if not value or not re.fullmatch(r"\+?[1-9][0-9 ()-]{6,23}", value):
        return None
    number = re.sub(r"[ ()-]", "", value).lstrip("+")
    return number if re.fullmatch(r"[1-9][0-9]{7,14}", number) else None


class PaymentFollowups:
    def __init__(self, orchestrator: Any) -> None:
        self.o = orchestrator
        self.db = orchestrator.db
        self.sealer = Sealer(
            orchestrator.settings.pseudonym_secret(), b"billing-followup-contacts-v1"
        )

    async def configure_contact(
        self,
        tenant: str,
        phone: str | None,
        allowed: bool,
        source: str,
        actor: str,
    ) -> dict[str, Any]:
        if phone is None:
            async with self.db.engine.connect() as conn:
                old = (
                    (await conn.execute(select(contacts).where(contacts.c.tenant == tenant)))
                    .mappings()
                    .first()
                )
            phone = self.sealer.open(tenant, old["phone_sealed"]) if old else None
        normalized = phone_number(phone)
        if normalized is None:
            raise BillingError("international WhatsApp number is required")
        if not 5 <= len(source.strip()) <= 128:
            raise BillingError("record the payment contact permission source")
        values = dict(
            phone_sealed=self.sealer.seal(tenant, normalized),
            whatsapp_allowed=allowed,
            consent_source=source.strip(),
            updated_at=utcnow(),
        )
        async with self.db.engine.begin() as conn:
            changed = await conn.execute(
                update(contacts).where(contacts.c.tenant == tenant).values(**values)
            )
            if changed.rowcount == 0:
                await conn.execute(insert(contacts).values(tenant=tenant, **values))
            await self.o.audit.record_in(
                conn, tenant, actor, "billing.contact.updated", "billing/contact"
            )
        return await self.contact(tenant)

    async def contact(self, tenant: str) -> dict[str, Any]:
        async with self.db.engine.connect() as conn:
            row = (
                (await conn.execute(select(contacts).where(contacts.c.tenant == tenant)))
                .mappings()
                .first()
            )
        if row is None:
            return {"configured": False, "whatsapp_allowed": False}
        phone = self.sealer.open(tenant, row["phone_sealed"])
        return {
            "configured": True,
            "whatsapp_allowed": row["whatsapp_allowed"],
            "phone_masked": "•••• " + phone[-4:],
            "consent_source": row["consent_source"],
        }

    async def _owned(self, conn: Any, tenant: str, invoice_id: str, platform: bool) -> Any:
        condition = and_(
            orders.c.invoice_id == invoice_id,
            orders.c.kind == ("subscription" if platform else "appointment"),
            orders.c.tenant == tenant,
        )
        row = (
            (await conn.execute(select(orders).where(condition).with_for_update()))
            .mappings()
            .first()
        )
        if row is None:
            raise KeyError("payment not found")
        return row

    async def _context(self, tenant: str, invoice_id: str, platform: bool) -> dict[str, Any]:
        async with self.db.engine.connect() as conn:
            order = dict(await self._owned(conn, tenant, invoice_id, platform))
            payer = order["tenant"]
            follow = (
                (
                    await conn.execute(
                        select(followups).where(
                            and_(followups.c.tenant == payer, followups.c.invoice_id == invoice_id)
                        )
                    )
                )
                .mappings()
                .first()
            )
            history = (
                (
                    await conn.execute(
                        select(events)
                        .where(and_(events.c.tenant == payer, events.c.invoice_id == invoice_id))
                        .order_by(events.c.created_at.desc())
                    )
                )
                .mappings()
                .all()
            )
            if platform:
                person = (
                    (await conn.execute(select(contacts).where(contacts.c.tenant == payer)))
                    .mappings()
                    .first()
                )
                phone = self.sealer.open(payer, person["phone_sealed"]) if person else None
                allowed = bool(person and person["whatsapp_allowed"])
                name, active = payer, True
                sender = self.o.settings.billing_platform_tenant
            else:
                person = (
                    (
                        await conn.execute(
                            select(patients).where(
                                and_(
                                    patients.c.tenant == payer, patients.c.id == order["patient_id"]
                                )
                            )
                        )
                    )
                    .mappings()
                    .first()
                )
                phone = person["phone"] if person else None
                name = person["display_name"] if person else "Ficha no disponible"
                visit = (
                    await conn.execute(
                        select(appointments.c.status).where(
                            and_(
                                appointments.c.tenant == payer,
                                appointments.c.id == order["resource_id"],
                            )
                        )
                    )
                ).scalar_one_or_none()
                active = bool(
                    person
                    and not person["restricted"]
                    and visit not in (None, "cancelled", "no_show")
                )
                if person and person["restricted"]:
                    name, phone = "Ficha no disponible", None
                allowed = False  # resolved below without sharing this connection
                sender = payer
        if not platform:
            allowed = await self.o.consents.has(
                payer, order["patient_id"], Purpose.WHATSAPP_BILLING
            )
        phone = phone_number(phone)
        opted_out = bool(phone and await self.o.inbound.is_opted_out(sender, "whatsapp", phone))
        return {
            "order": order,
            "name": name,
            "phone": phone,
            "allowed": allowed,
            "active": active,
            "opted_out": opted_out,
            "sender": sender,
            "follow": dict(follow) if follow else {},
            "history": [dict(r) for r in history],
            "automatic": await self.o.billing_automation.target(tenant, "order:" + invoice_id)
            if not platform
            else None,
        }

    def _reason(self, context: dict[str, Any], now: datetime) -> str | None:
        order, follow = context["order"], context["follow"]
        if order["state"] == "paid":
            return "paid"
        if not self.o.settings.billing_enabled:
            return "payments_not_enabled"
        if order["state"] not in ("pending", "rejected"):
            return "payment_needs_review"
        if not context["active"]:
            return "inactive_record"
        if follow.get("paused"):
            return "paused"
        if not context["phone"]:
            return "phone_missing"
        if not context["allowed"]:
            return "permission_missing"
        if context["opted_out"]:
            return "opted_out"
        next_at = aware(follow.get("next_contact_at"))
        promise = aware(follow.get("promised_for"))
        if promise and promise > now:
            return "payment_promised"
        if next_at and next_at > now:
            return "contact_not_due"
        return None

    def _public(self, context: dict[str, Any]) -> dict[str, Any]:
        order, follow = context["order"], context["follow"]
        history = [
            {k: event[k] for k in ("event_id", "state", "created_at", "confirmed_at", "actor")}
            for event in context["history"]
        ]
        for event in history:
            event["created_at"] = aware(event["created_at"])
            event["confirmed_at"] = aware(event["confirmed_at"])
        return {
            "invoice_id": order["invoice_id"],
            "payer_tenant": order["tenant"],
            "kind": order["kind"],
            "subject_id": order["patient_id"],
            "whatsapp_allowed": context["allowed"],
            "name": context["name"],
            "amount_cents": order["amount_cents"],
            "state": order["state"],
            "phone_masked": "•••• " + context["phone"][-4:] if context["phone"] else None,
            "blocked_reason": self._reason(context, utcnow()),
            "next_contact_at": aware(follow.get("next_contact_at")),
            "promised_for": aware(follow.get("promised_for")),
            "paused": follow.get("paused", False),
            "history": history,
            "automatic": context.get("automatic"),
            "can_automate": order["kind"] == "appointment" and order["method"] == "card",
        }

    async def listing(
        self, tenant: str, *, platform: bool = False, after: str = ""
    ) -> dict[str, Any]:
        where = [
            orders.c.kind == ("subscription" if platform else "appointment"),
            orders.c.state != "paid",
        ]
        if not platform:
            where.append(orders.c.tenant == tenant)
        if after:
            parts = after.split("/")
            if (
                len(parts) != 2
                or not re.fullmatch(r"[\w.-]{1,64}", parts[0])
                or not re.fullmatch(r"[0-9a-f]{32}", parts[1])
            ):
                raise BillingError("invalid follow-up cursor")
            where.append(
                or_(
                    orders.c.tenant > parts[0],
                    and_(orders.c.tenant == parts[0], orders.c.invoice_id > parts[1]),
                )
            )
        async with self.db.engine.connect() as conn:
            rows = (
                await conn.execute(
                    select(orders.c.tenant, orders.c.invoice_id)
                    .where(and_(*where))
                    .order_by(orders.c.tenant, orders.c.invoice_id)
                    .limit(101)
                )
            ).all()
        items = []
        for payer, invoice_id in rows[:100]:
            context = await self._context(payer, invoice_id, platform)
            if context["order"]["state"] != "paid":
                items.append(self._public(context))
        cursor = f"{rows[99].tenant}/{rows[99].invoice_id}" if len(rows) > 100 else None
        return {"items": items, "next_cursor": cursor, "delivery_mode": "manual_whatsapp"}

    async def get(self, tenant: str, invoice_id: str, *, platform: bool = False) -> dict[str, Any]:
        return self._public(await self._context(tenant, invoice_id, platform))

    async def schedule(
        self,
        tenant: str,
        invoice_id: str,
        actor: str,
        *,
        platform: bool = False,
        next_contact_at: datetime | None = None,
        promised_for: datetime | None = None,
        paused: bool = False,
    ) -> dict[str, Any]:
        for value in (next_contact_at, promised_for):
            if value is not None and value.tzinfo is None:
                raise BillingError("follow-up date requires a timezone")
        async with self.db.engine.begin() as conn:
            row = await self._owned(conn, tenant, invoice_id, platform)
            payer = row["tenant"]
            key = and_(followups.c.tenant == payer, followups.c.invoice_id == invoice_id)
            values = dict(
                next_contact_at=next_contact_at,
                promised_for=promised_for,
                paused=paused,
                updated_at=utcnow(),
            )
            if (await conn.execute(update(followups).where(key).values(**values))).rowcount == 0:
                await conn.execute(
                    insert(followups).values(tenant=payer, invoice_id=invoice_id, **values)
                )
            await self.o.audit.record_in(
                conn,
                payer,
                actor,
                "billing.followup.scheduled",
                f"billing/{invoice_id}",
                subject_id=row["patient_id"],
            )
        return self._public(await self._context(tenant, invoice_id, platform))

    async def prepare(
        self, tenant: str, invoice_id: str, actor: str, *, platform: bool = False
    ) -> dict[str, Any]:
        self.o.billing._enabled()
        context = await self._context(tenant, invoice_id, platform)
        automatic = context.get("automatic")
        if automatic and (automatic["enabled"] or automatic["state"] in ("sending", "uncertain")):
            raise BillingError("automatic reminders are active or require delivery reconciliation")
        order = context["order"]
        if order["method"] == "card":
            if platform:
                raise BillingError("reconcile the monthly card plan before contacting the payer")
            await self.o.dlocal_billing.reconcile_payment(order["tenant"], invoice_id)
            context = await self._context(tenant, invoice_id, platform)
        reason = self._reason(context, utcnow())
        if reason:
            raise BillingError(f"WhatsApp follow-up blocked: {reason}")
        payer = order["tenant"]
        fingerprint = address_key(
            self.o.settings.pseudonym_secret(), payer, "whatsapp", context["phone"]
        )
        try:
            async with self.db.engine.begin() as conn:
                current = await self._owned(conn, tenant, invoice_id, platform)
                if current["state"] not in ("pending", "rejected"):
                    raise BillingError("payment changed; reload before contacting")
                old = (
                    (
                        await conn.execute(
                            select(events).where(
                                and_(
                                    events.c.tenant == payer,
                                    events.c.invoice_id == invoice_id,
                                    events.c.open_slot == "open",
                                )
                            )
                        )
                    )
                    .mappings()
                    .first()
                )
                if old:
                    if old["address_key"] != fingerprint:
                        raise BillingError("contact changed; discard the previous draft")
                    event_id = old["event_id"]
                else:
                    event_id = uuid.uuid4().hex
                    await conn.execute(
                        insert(events).values(
                            tenant=payer,
                            event_id=event_id,
                            invoice_id=invoice_id,
                            state="prepared",
                            open_slot="open",
                            address_key=fingerprint,
                            actor=actor,
                            created_at=utcnow(),
                        )
                    )
                    await self.o.audit.record_in(
                        conn,
                        payer,
                        actor,
                        "billing.whatsapp.prepared",
                        f"billing/{invoice_id}",
                        subject_id=current["patient_id"],
                    )
        except IntegrityError:
            raise BillingError("follow-up conflicted; reload before contacting") from None
        amount = f"USD {order['amount_cents'] // 100}.{order['amount_cents'] % 100:02d}"
        description = "mensualidad de Tatos" if platform else "pago"
        text = (
            f"Hola. Te contactamos por tu {description} pendiente de {amount}, "
            f"referencia {invoice_id[:8]}. Puedes revisar su estado en Tatos. "
            "Si ya pagaste, avísanos para verificarlo. Gracias."
        )
        return {
            "event_id": event_id,
            "state": "prepared",
            "message": text,
            "whatsapp_url": f"https://wa.me/{context['phone']}?{urlencode({'text': text})}",
            "sent": False,
        }

    async def complete(
        self,
        tenant: str,
        invoice_id: str,
        event_id: str,
        actor: str,
        sent: bool,
        *,
        platform: bool = False,
    ) -> dict[str, Any]:
        async with self.db.engine.begin() as conn:
            order = await self._owned(conn, tenant, invoice_id, platform)
            payer = order["tenant"]
            key = and_(
                events.c.tenant == payer,
                events.c.invoice_id == invoice_id,
                events.c.event_id == event_id,
            )
            event = (
                (await conn.execute(select(events).where(key).with_for_update())).mappings().first()
            )
            if event is None:
                raise KeyError("follow-up draft not found")
            state = "manual_sent" if sent else "discarded"
            if event["state"] != "prepared":
                if event["state"] != state:
                    raise BillingError("follow-up draft is already closed")
                return {"state": state, "delivery_verified": False}
            now = utcnow()
            await conn.execute(
                update(events)
                .where(key)
                .values(state=state, open_slot=None, actor=actor, confirmed_at=now)
            )
            if sent:
                follow_key = and_(followups.c.tenant == payer, followups.c.invoice_id == invoice_id)
                previous = (
                    await conn.execute(
                        select(followups.c.next_contact_at).where(follow_key).with_for_update()
                    )
                ).scalar_one_or_none()
                next_at = max(now + timedelta(days=3), aware(previous) or now)
                values = dict(next_contact_at=next_at, updated_at=now)
                if (
                    await conn.execute(update(followups).where(follow_key).values(**values))
                ).rowcount == 0:
                    await conn.execute(
                        insert(followups).values(
                            tenant=payer, invoice_id=invoice_id, paused=False, **values
                        )
                    )
            await self.o.audit.record_in(
                conn,
                payer,
                actor,
                f"billing.whatsapp.{state}",
                f"billing/{invoice_id}",
                subject_id=order["patient_id"],
            )
        return {"state": state, "delivery_verified": False}
