"""Durable invoices and human-reviewed bank transfers. Card processing is not enabled
here: provider-backed card verification is a separate adapter, never a client status.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
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

from orchestrator.billing import (
    BillingError,
    BillingPolicy,
    ChargeKind,
    Invoice,
    PaymentMethod,
    PaymentState,
    Recipient,
)
from orchestrator.config import Settings
from orchestrator.crm import appointments, patients
from orchestrator.db import Database, metadata, utcnow
from orchestrator.governance import AuditLog

payees = Table(
    "billing_payees",
    metadata,
    Column("tenant", String(64), primary_key=True),
    Column("scope", String(16), primary_key=True),  # clinic | platform
    Column("merchant_id", String(64), nullable=False, unique=True),
    Column("instructions", Text, nullable=False),  # bank details deliberately shown to payer
    Column("updated_at", DateTime(timezone=True), nullable=False),
)
orders = Table(
    "billing_orders",
    metadata,
    Column("tenant", String(64), primary_key=True),
    Column("invoice_id", String(32), primary_key=True),
    Column("kind", String(16), nullable=False),
    Column("resource_id", String(64), nullable=False),
    Column("patient_id", String(64), nullable=True),
    Column("merchant_id", String(64), nullable=False),
    Column("amount_cents", BigInteger, nullable=False),
    Column("currency", String(3), nullable=False),
    Column("method", String(16), nullable=False),
    Column("state", String(24), nullable=False),
    Column("bank_instructions", Text, nullable=False),  # immutable destination snapshot
    Column("transfer_reference", String(120), nullable=True),
    Column("review_note", String(200), nullable=True),
    Column("settlement_id", String(120), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("tenant", "kind", "resource_id", name="uq_billing_receivable"),
    UniqueConstraint("merchant_id", "settlement_id", name="uq_billing_bank_settlement"),
)


class BillingStore:
    def __init__(self, db: Database, audit: AuditLog, settings: Settings) -> None:
        self.db, self.audit, self.settings = db, audit, settings

    def operator(self, tenant: str, *, service: bool) -> bool:
        return bool(
            service
            and self.settings.billing_platform_tenant
            and tenant == self.settings.billing_platform_tenant
        )

    def _enabled(self) -> None:
        if not self.settings.billing_enabled:
            raise BillingError("payments are not enabled")

    def _scope(self, tenant: str, scope: str, operator: bool) -> None:
        if scope not in ("clinic", "platform"):
            raise BillingError("unknown payment scope")
        if scope == "platform" and (
            not operator or tenant != self.settings.billing_platform_tenant
        ):
            raise BillingError("platform operator access is required")

    async def configure_bank(
        self,
        tenant: str,
        scope: str,
        instructions: str,
        actor: str,
        *,
        operator: bool = False,
    ) -> dict[str, Any]:
        self._scope(tenant, scope, operator)
        instructions = instructions.strip()
        if not 10 <= len(instructions) <= 1500:
            raise BillingError("bank instructions must contain 10 to 1500 characters")
        async with self.db.engine.begin() as conn:
            row = (
                (
                    await conn.execute(
                        select(payees)
                        .where(and_(payees.c.tenant == tenant, payees.c.scope == scope))
                        .with_for_update()
                    )
                )
                .mappings()
                .first()
            )
            if row:
                await conn.execute(
                    update(payees)
                    .where(and_(payees.c.tenant == tenant, payees.c.scope == scope))
                    .values(instructions=instructions, updated_at=utcnow())
                )
            else:
                await conn.execute(
                    insert(payees).values(
                        tenant=tenant,
                        scope=scope,
                        merchant_id=uuid.uuid4().hex,
                        instructions=instructions,
                        updated_at=utcnow(),
                    )
                )
            await self.audit.record_in(
                conn, tenant, actor, "billing.bank.configured", f"billing/{scope}"
            )
        return {"scope": scope, "configured": True}

    async def configuration(self, tenant: str, *, operator: bool = False) -> dict[str, Any]:
        async with self.db.engine.connect() as conn:
            clinic = await self._payee(conn, tenant, "clinic")
            platform = await self._payee(conn, self.settings.billing_platform_tenant, "platform")
        return {
            "enabled": self.settings.billing_enabled,
            "currency": "USD",
            "clinic_bank_configured": clinic is not None,
            "platform_bank_configured": platform is not None,
            "monthly_price_cents": self.settings.billing_monthly_price_cents or None,
            "platform_operator": operator,
            "automatic_card_available": False,
            "clinic_bank_instructions": clinic["instructions"] if clinic else "",
            "platform_bank_instructions": platform["instructions"] if platform else "",
        }

    async def _payee(self, conn: Any, tenant: str, scope: str) -> Any:
        return (
            (
                await conn.execute(
                    select(payees).where(and_(payees.c.tenant == tenant, payees.c.scope == scope))
                )
            )
            .mappings()
            .first()
        )

    async def create(
        self,
        tenant: str,
        kind: ChargeKind,
        resource_id: str | None,
        actor: str,
        *,
        patient_id: str | None = None,
    ) -> dict[str, Any]:
        if kind not in (ChargeKind.APPOINTMENT, ChargeKind.SUBSCRIPTION):
            raise BillingError("unknown charge kind")
        if kind == ChargeKind.SUBSCRIPTION:
            resource_id = utcnow().strftime("%Y-%m")
        try:
            return await self._create(tenant, kind, resource_id, actor, patient_id=patient_id)
        except IntegrityError:
            async with self.db.engine.connect() as conn:
                row = (
                    (
                        await conn.execute(
                            select(orders).where(
                                and_(
                                    orders.c.tenant == tenant,
                                    orders.c.kind == kind,
                                    orders.c.resource_id == resource_id,
                                )
                            )
                        )
                    )
                    .mappings()
                    .first()
                )
            if row is None or (patient_id is not None and row["patient_id"] != patient_id):
                raise
            return self._public(row)

    async def _create(
        self,
        tenant: str,
        kind: ChargeKind,
        resource_id: str | None,
        actor: str,
        *,
        patient_id: str | None = None,
    ) -> dict[str, Any]:
        self._enabled()
        async with self.db.engine.begin() as conn:
            subject = None
            if kind == ChargeKind.APPOINTMENT:
                query = (
                    select(
                        appointments.c.id,
                        appointments.c.patient_id,
                        appointments.c.price,
                        appointments.c.status,
                    )
                    .join(
                        patients,
                        and_(
                            patients.c.tenant == appointments.c.tenant,
                            patients.c.id == appointments.c.patient_id,
                        ),
                    )
                    .where(
                        and_(
                            appointments.c.tenant == tenant,
                            appointments.c.id == resource_id,
                            patients.c.restricted.is_(False),
                        )
                    )
                )
                if patient_id is not None:
                    query = query.where(appointments.c.patient_id == patient_id)
                visit = (await conn.execute(query.with_for_update())).mappings().first()
                if visit is None:
                    raise KeyError("appointment not found")
                if visit["status"] in ("cancelled", "no_show"):
                    raise BillingError("this appointment cannot be charged")
                subject = visit["patient_id"]
                receiver = await self._payee(conn, tenant, "clinic")
                price = Decimal(visit["price"])
            else:
                if patient_id is not None:
                    raise BillingError("patient access cannot buy a clinic subscription")
                kind = ChargeKind.SUBSCRIPTION
                receiver = await self._payee(
                    conn, self.settings.billing_platform_tenant, "platform"
                )
                price = None
            if receiver is None:
                raise BillingError("recipient bank account is not configured")
            old = (
                (
                    await conn.execute(
                        select(orders).where(
                            and_(
                                orders.c.tenant == tenant,
                                orders.c.kind == kind,
                                orders.c.resource_id == resource_id,
                            )
                        )
                    )
                )
                .mappings()
                .first()
            )
            if old:
                return self._public(old)
            invoice_id = uuid.uuid4().hex
            if kind == ChargeKind.APPOINTMENT:
                assert price is not None and resource_id is not None
                policy = BillingPolicy(
                    Recipient("unused-platform", None),
                    (Recipient(receiver["merchant_id"], tenant),),
                )
                invoice = Invoice.appointment(
                    policy, tenant, invoice_id, resource_id, price, PaymentMethod.TRANSFER
                )
            else:
                policy = BillingPolicy(
                    Recipient(receiver["merchant_id"], None),
                    monthly_price_cents=self.settings.billing_monthly_price_cents or None,
                )
                assert resource_id is not None
                invoice = Invoice.subscription(
                    policy, tenant, invoice_id, resource_id, PaymentMethod.TRANSFER
                )
            values = {
                k: v
                for k, v in asdict(invoice).items()
                if k not in ("attempt_id", "provider_transaction_id")
            }
            now = utcnow()
            await conn.execute(
                insert(orders).values(
                    **values,
                    patient_id=subject,
                    bank_instructions=receiver["instructions"],
                    created_at=now,
                    updated_at=now,
                )
            )
            await self.audit.record_in(
                conn,
                tenant,
                actor,
                "billing.invoice.created",
                f"billing/{invoice_id}",
                subject_id=subject,
            )
            row = (
                (
                    await conn.execute(
                        select(orders).where(
                            and_(orders.c.tenant == tenant, orders.c.invoice_id == invoice_id)
                        )
                    )
                )
                .mappings()
                .one()
            )
            return self._public(row)

    @staticmethod
    def _public(row: Any) -> dict[str, Any]:
        return {
            k: row[k]
            for k in (
                "invoice_id",
                "kind",
                "resource_id",
                "amount_cents",
                "currency",
                "method",
                "state",
                "bank_instructions",
                "transfer_reference",
                "review_note",
                "created_at",
            )
        }

    async def listing(
        self,
        tenant: str,
        kind: ChargeKind,
        *,
        patient_id: str | None = None,
        platform_review: bool = False,
    ) -> list[dict[str, Any]]:
        where = [orders.c.kind == kind]
        if not platform_review:
            where.append(orders.c.tenant == tenant)
        elif kind != ChargeKind.SUBSCRIPTION:
            raise BillingError("platform review only exposes app subscriptions")
        if patient_id is not None:
            if kind != ChargeKind.APPOINTMENT:
                raise BillingError("patients cannot read app subscriptions")
            where.append(orders.c.patient_id == patient_id)
        async with self.db.engine.connect() as conn:
            rows = (
                (
                    await conn.execute(
                        select(orders)
                        .where(and_(*where))
                        .order_by(orders.c.created_at.desc())
                        .limit(200)
                    )
                )
                .mappings()
                .all()
            )
        return [
            {**self._public(r), **({"payer_tenant": r["tenant"]} if platform_review else {})}
            for r in rows
        ]

    async def export_subject(self, tenant: str, patient_id: str) -> list[dict[str, Any]]:
        """Complete rights export, without the UI's 200-row limit or other patients.

        Platform subscriptions belong to the clinic, never to an individual patient.
        No provider credentials or internal settlement identifiers are exposed.
        """
        async with self.db.engine.connect() as conn:
            rows = (
                (
                    await conn.execute(
                        select(orders)
                        .where(
                            and_(
                                orders.c.tenant == tenant,
                                orders.c.patient_id == patient_id,
                                orders.c.kind == ChargeKind.APPOINTMENT,
                            )
                        )
                        .order_by(orders.c.created_at, orders.c.invoice_id)
                    )
                )
                .mappings()
                .all()
            )
        return [self._public(row) for row in rows]

    async def notify_transfer(
        self,
        tenant: str,
        invoice_id: str,
        reference: str,
        actor: str,
        *,
        patient_id: str | None = None,
    ) -> dict[str, Any]:
        self._enabled()
        reference = reference.strip()
        if not 3 <= len(reference) <= 120:
            raise BillingError("transfer reference must contain 3 to 120 characters")
        async with self.db.engine.begin() as conn:
            row = await self._row(conn, tenant, invoice_id, patient_id)
            invoice = self._invoice(row).notify_transfer()
            if row["state"] == PaymentState.TRANSFER_REVIEW:
                if row["transfer_reference"] != reference:
                    raise BillingError("a transfer is already being reviewed")
                return self._public(row)
            await conn.execute(
                update(orders)
                .where(and_(orders.c.tenant == tenant, orders.c.invoice_id == invoice_id))
                .values(
                    state=invoice.state,
                    transfer_reference=reference,
                    review_note=None,
                    updated_at=utcnow(),
                )
            )
            await self.audit.record_in(
                conn,
                tenant,
                actor,
                "billing.transfer.reported",
                f"billing/{invoice_id}",
                subject_id=row["patient_id"],
            )
            return self._public(await self._row(conn, tenant, invoice_id, patient_id))

    async def review_transfer(
        self,
        tenant: str,
        invoice_id: str,
        actor: str,
        approved: bool,
        note: str,
        *,
        platform_operator: bool = False,
        bank_transaction_id: str | None = None,
    ) -> dict[str, Any]:
        try:
            return await self._review_transfer(
                tenant,
                invoice_id,
                actor,
                approved,
                note,
                platform_operator=platform_operator,
                bank_transaction_id=bank_transaction_id,
            )
        except IntegrityError as exc:
            raise BillingError(
                "verification conflicted; check whether this bank transaction was already used"
            ) from exc

    async def _review_transfer(
        self,
        tenant: str,
        invoice_id: str,
        actor: str,
        approved: bool,
        note: str,
        *,
        platform_operator: bool = False,
        bank_transaction_id: str | None = None,
    ) -> dict[str, Any]:
        self._enabled()
        if not 5 <= len(note.strip()) <= 200:
            raise BillingError("record a verification or rejection note, 5 to 200 characters")
        if approved and (
            not bank_transaction_id or not 3 <= len(bank_transaction_id.strip()) <= 120
        ):
            raise BillingError("verified bank transaction id is required")
        async with self.db.engine.begin() as conn:
            row = await self._row(conn, tenant, invoice_id)
            invoice = self._invoice(row)
            if platform_operator and invoice.kind != ChargeKind.SUBSCRIPTION:
                raise BillingError("platform operator cannot review clinic receivables")
            reviewer = None if platform_operator else tenant
            result = invoice.review_transfer(reviewer_tenant=reviewer, approved=approved)
            await conn.execute(
                update(orders)
                .where(and_(orders.c.tenant == tenant, orders.c.invoice_id == invoice_id))
                .values(
                    state=result.state,
                    review_note=note.strip(),
                    settlement_id=bank_transaction_id.strip()
                    if approved and bank_transaction_id
                    else None,
                    updated_at=utcnow(),
                )
            )
            await self.audit.record_in(
                conn,
                tenant,
                actor,
                "billing.transfer.approved" if approved else "billing.transfer.rejected",
                f"billing/{invoice_id}",
                subject_id=row["patient_id"],
            )
            return self._public(await self._row(conn, tenant, invoice_id))

    async def _row(
        self, conn: Any, tenant: str, invoice_id: str, patient_id: str | None = None
    ) -> Any:
        query = select(orders).where(
            and_(orders.c.tenant == tenant, orders.c.invoice_id == invoice_id)
        )
        if patient_id is not None:
            query = query.where(
                orders.c.patient_id == patient_id, orders.c.kind == ChargeKind.APPOINTMENT
            )
        row = (await conn.execute(query.with_for_update())).mappings().first()
        if row is None:
            raise KeyError("invoice not found")
        return row

    @staticmethod
    def _invoice(row: Any) -> Invoice:
        return Invoice(
            row["tenant"],
            row["invoice_id"],
            ChargeKind(row["kind"]),
            row["resource_id"],
            row["merchant_id"],
            row["amount_cents"],
            PaymentMethod(row["method"]),
            row["currency"],
            PaymentState(row["state"]),
        )
