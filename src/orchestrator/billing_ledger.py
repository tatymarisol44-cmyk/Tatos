"""Append-only human-confirmed bank credits, exact balances and explicit deadlines.

A patient reporting a transfer never enters this ledger. Only the recipient's
authorized reviewer confirms an observed bank movement. No bank API is implied.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    Index,
    String,
    Table,
    UniqueConstraint,
    insert,
    select,
    update,
)
from sqlalchemy.exc import IntegrityError

from orchestrator.billing import BillingError
from orchestrator.billing_store import orders, payees
from orchestrator.db import aware, metadata, utcnow

if TYPE_CHECKING:
    from orchestrator.billing_store import BillingStore

movements = Table(
    "billing_movements",
    metadata,
    Column("tenant", String(64), primary_key=True),
    Column("id", String(32), primary_key=True),
    Column("invoice_id", String(32), nullable=False),
    Column("merchant_id", String(64), nullable=False),
    Column("request_id", String(64), nullable=False),
    Column("bank_transaction_id", String(120), nullable=False),
    Column("amount_cents", BigInteger, nullable=False),
    Column("note", String(200), nullable=False),
    Column("actor", String(128), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("tenant", "request_id", name="uq_billing_movement_request"),
    UniqueConstraint("merchant_id", "bank_transaction_id", name="uq_billing_movement_bank"),
    Index("ix_billing_movements_invoice", "tenant", "invoice_id"),
)


async def lock_recipient(conn: Any, tenant: str, invoice_id: str) -> None:
    merchant = (
        await conn.execute(
            select(orders.c.merchant_id).where(
                orders.c.tenant == tenant,
                orders.c.invoice_id == invoice_id,
            )
        )
    ).scalar_one_or_none()
    if merchant is None:
        raise KeyError("invoice not found")
    # Shared with full-transfer review: a bank movement cannot settle two orders.
    await conn.execute(
        select(payees.c.merchant_id)
        .where(
            payees.c.merchant_id == merchant,
        )
        .with_for_update()
    )


async def append_payment(
    conn: Any, row: Any, amount: int, bank_id: str, request_id: str, actor: str, note: str
) -> None:
    bank_id = bank_id.strip()
    if type(amount) is not int or not 0 < amount <= row["amount_cents"] - row["credited_cents"]:
        raise BillingError(
            "confirmed amount must be positive and no more than the remaining balance"
        )
    if not 3 <= len(bank_id) <= 120 or not 5 <= len(note.strip()) <= 200:
        raise BillingError("record the bank movement id and a verification note")
    legacy = (
        await conn.execute(
            select(orders.c.invoice_id).where(
                orders.c.merchant_id == row["merchant_id"],
                orders.c.settlement_id == bank_id,
            )
        )
    ).first()
    if legacy:
        raise BillingError("this bank movement was already used for a verified payment")
    await conn.execute(
        insert(movements).values(
            tenant=row["tenant"],
            id=uuid.uuid4().hex,
            invoice_id=row["invoice_id"],
            merchant_id=row["merchant_id"],
            request_id=request_id,
            bank_transaction_id=bank_id,
            amount_cents=amount,
            note=note.strip(),
            actor=actor,
            created_at=utcnow(),
        )
    )


class BillingLedger:
    def __init__(self, billing: BillingStore) -> None:
        self.billing, self.db, self.audit = billing, billing.db, billing.audit

    async def statement(
        self, tenant: str, invoice_id: str, *, platform: bool = False
    ) -> dict[str, Any]:
        async with self.db.engine.connect() as conn:
            row = await self.billing._row(conn, tenant, invoice_id)
            if platform != (row["kind"] == "subscription"):
                raise KeyError("payment scope not found")
            history = (
                (
                    await conn.execute(
                        select(movements)
                        .where(
                            movements.c.tenant == tenant,
                            movements.c.invoice_id == invoice_id,
                        )
                        .order_by(movements.c.created_at, movements.c.id)
                    )
                )
                .mappings()
                .all()
            )
        public = self.billing._public(row)
        due = aware(row["due_at"])
        outstanding = public["balance_cents"]
        overdue = bool(due and due < utcnow() and outstanding > 0)
        state = (
            "paid"
            if row["state"] == "paid"
            else "cancelled"
            if row["state"] == "cancelled"
            else "partial_overdue"
            if overdue and row["credited_cents"]
            else "overdue"
            if overdue
            else "partial"
            if row["credited_cents"]
            else "pending"
        )
        return {
            **public,
            "balance_state": state,
            "revision": row["payment_revision"],
            "movements": [
                {
                    "created_at": aware(item["created_at"]),
                    **{
                        k: item[k]
                        for k in (
                            "id",
                            "amount_cents",
                            "bank_transaction_id",
                            "note",
                            "actor",
                        )
                    },
                }
                for item in history
            ],
            "evidence": "human_confirmed_bank_movements"
            if history
            else "provider_or_legacy_record"
            if row["state"] == "paid"
            else "no_confirmed_payment",
        }

    async def deadline(
        self,
        tenant: str,
        invoice_id: str,
        due: datetime | None,
        revision: int,
        actor: str,
        *,
        platform: bool = False,
    ) -> dict[str, Any]:
        self.billing._enabled()
        if due is not None and due.tzinfo is None:
            raise BillingError("payment due date requires a timezone")
        from orchestrator.billing_automation import targets

        async with self.db.engine.begin() as conn:
            row = await self.billing._row(conn, tenant, invoice_id)
            if platform != (row["kind"] == "subscription"):
                raise KeyError("payment scope not found")
            automatic = (
                (
                    await conn.execute(
                        select(targets).where(
                            targets.c.tenant == tenant,
                            targets.c.source_key == "order:" + invoice_id,
                        )
                    )
                )
                .mappings()
                .first()
            )
            if automatic and (
                automatic["enabled"] or automatic["state"] in {"sending", "uncertain"}
            ):
                raise BillingError("change the deadline through the active automation settings")
            changed = await conn.execute(
                update(orders)
                .where(
                    orders.c.tenant == tenant,
                    orders.c.invoice_id == invoice_id,
                    orders.c.payment_revision == revision,
                )
                .values(due_at=aware(due), payment_revision=revision + 1, updated_at=utcnow())
            )
            if changed.rowcount != 1:
                raise BillingError("payment changed; reload before changing its deadline")
            await self.audit.record_in(
                conn,
                tenant,
                actor,
                "billing.deadline.updated",
                f"billing/{invoice_id}",
                subject_id=row["patient_id"],
            )
        return await self.statement(tenant, invoice_id, platform=platform)

    async def credit(
        self,
        tenant: str,
        invoice_id: str,
        amount: int,
        bank_id: str,
        request_id: str,
        note: str,
        actor: str,
        *,
        platform: bool = False,
    ) -> dict[str, Any]:
        self.billing._enabled()
        try:
            async with self.db.engine.begin() as conn:
                await lock_recipient(conn, tenant, invoice_id)
                await self.billing._manual_subscription_allowed(conn, tenant, invoice_id=invoice_id)
                row = await self.billing._row(conn, tenant, invoice_id)
                if platform != (row["kind"] == "subscription"):
                    raise KeyError("payment scope not found")
                previous = (
                    (
                        await conn.execute(
                            select(movements).where(
                                movements.c.tenant == tenant,
                                movements.c.request_id == request_id,
                            )
                        )
                    )
                    .mappings()
                    .first()
                )
                if previous:
                    if (
                        previous["invoice_id"],
                        previous["amount_cents"],
                        previous["bank_transaction_id"],
                    ) != (invoice_id, amount, bank_id.strip()):
                        raise BillingError(
                            "the request id already identifies a different bank movement"
                        )
                else:
                    if row["method"] != "transfer" or row["state"] not in {
                        "pending",
                        "rejected",
                        "transfer_review",
                    }:
                        raise BillingError(
                            "reconcile any card checkout or completed payment "
                            "before recording an abono"
                        )
                    await append_payment(conn, row, amount, bank_id, request_id, actor, note)
                    credited = row["credited_cents"] + amount
                    settled = credited == row["amount_cents"]
                    await conn.execute(
                        update(orders)
                        .where(
                            orders.c.tenant == tenant,
                            orders.c.invoice_id == invoice_id,
                        )
                        .values(
                            credited_cents=credited,
                            state="paid" if settled else "pending",
                            settlement_id=bank_id.strip() if settled else None,
                            transfer_reference=None,
                            review_note=note.strip(),
                            payment_revision=row["payment_revision"] + 1,
                            updated_at=utcnow(),
                        )
                    )
                    await self.audit.record_in(
                        conn,
                        tenant,
                        actor,
                        "billing.credit.settled" if settled else "billing.credit.partial",
                        f"billing/{invoice_id}",
                        subject_id=row["patient_id"],
                        details={"amount_cents": amount, "evidence": "human_confirmation"},
                    )
                    from orchestrator.billing_followup import events

                    await conn.execute(
                        update(events)
                        .where(
                            events.c.tenant == tenant,
                            events.c.invoice_id == invoice_id,
                            events.c.state == "prepared",
                        )
                        .values(state="discarded", open_slot=None, confirmed_at=utcnow())
                    )
        except IntegrityError as exc:
            raise BillingError(
                "the bank movement or request was already used; reload the ledger"
            ) from exc
        return await self.statement(tenant, invoice_id, platform=platform)
