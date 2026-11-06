"""Private-only notice scenarios. No sender, provider credential or live switch exists.

Reuses authoritative invoices, ledger, permission/opt-out and contact resolution.
Every trial says simulated. The existing card reconciliation worker stays separate.
"""

from __future__ import annotations

import string
import uuid
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    Integer,
    String,
    Table,
    UniqueConstraint,
    insert,
    select,
    update,
)
from sqlalchemy.exc import IntegrityError

from orchestrator.billing import BillingError
from orchestrator.db import aware, metadata, utcnow
from orchestrator.guardrails import redact_pii

DEFAULT_TEXTS = {
    "before_due": "Tu pago de {balance} vence pronto. Revisa la referencia {reference} en la app.",
    "due": "Tu pago de {balance} está pendiente hoy. Revisa su estado en la app: {reference}.",
    "post_1": "Hay un saldo pendiente de {balance}. Si ya pagaste, solicita verificarlo en la app.",
    "post_2": "Recuerda revisar el saldo de {balance} de la referencia {reference} en la app.",
    "post_3": "Puedes revisar tu pago pendiente de {balance} "
    "o acordar su revisión con el consultorio.",
    "paid": "Se confirmó el pago de la referencia {reference}. Tu saldo es {balance}.",
    "partial": "Se confirmó un abono. Tu saldo restante es {balance}. Referencia: {reference}.",
    "escalation": "Se alcanzó el límite de avisos de {reference}; "
    "requiere revisión del administrador.",
}


def _money(cents: int) -> str:
    return f"USD {cents // 100}.{cents % 100:02d}"


notice_policies = Table(
    "billing_notice_policies",
    metadata,
    Column("tenant", String(64), primary_key=True),
    Column("scope", String(16), primary_key=True),
    Column("revision", Integer, nullable=False),
    Column("policy", JSON, nullable=False),
)
notice_trials = Table(
    "billing_notice_trials",
    metadata,
    Column("tenant", String(64), primary_key=True),
    Column("id", String(32), primary_key=True),
    Column("invoice_id", String(32), nullable=False),
    Column("event_key", String(100), nullable=False),
    Column("phase", String(24), nullable=False),
    Column("text", String(1000), nullable=False),
    Column("simulated_at", DateTime(timezone=True), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("tenant", "invoice_id", "event_key", name="uq_billing_notice_trial"),
)


class NoticePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    texts: dict[str, str] = Field(default_factory=lambda: dict(DEFAULT_TEXTS))
    cadence_days: int = Field(default=1, ge=1, le=30)
    maximum: int = Field(default=3, ge=1, le=3)
    start_hour: int = Field(default=9, ge=0, le=23)
    end_hour: int = Field(default=18, ge=1, le=24)
    before_hours: int = Field(default=24, ge=1, le=168)

    @model_validator(mode="after")
    def valid_templates(self) -> NoticePolicy:
        if self.start_hour >= self.end_hour or set(self.texts) != set(DEFAULT_TEXTS):
            raise ValueError("choose valid hours and all notice phases")
        for text in self.texts.values():
            if not 1 <= len(text.strip()) <= 600 or redact_pii(text)[1]:
                raise ValueError("use public courtesy text without personal data")
            try:
                for _, name, spec, conversion in string.Formatter().parse(text):
                    if name is not None and (
                        name not in {"balance", "amount", "reference"} or spec or conversion
                    ):
                        raise ValueError(
                            "only balance, amount and reference placeholders are allowed"
                        )
            except ValueError as exc:
                raise ValueError("invalid notice placeholders") from exc
        return self


class BillingNoticeTrials:
    def __init__(self, orchestrator: Any) -> None:
        self.o, self.db = orchestrator, orchestrator.db

    def private_only(self) -> None:
        if self.o.settings.app_env == "prod":
            raise BillingError("notice trials are available only in a private test environment")

    async def configuration(self, tenant: str, platform: bool = False) -> dict[str, Any]:
        scope = "platform" if platform else "clinic"
        async with self.db.engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        select(notice_policies).where(
                            notice_policies.c.tenant == tenant,
                            notice_policies.c.scope == scope,
                        )
                    )
                )
                .mappings()
                .first()
            )
        return {
            "revision": row["revision"] if row else 0,
            "policy": row["policy"] if row else NoticePolicy().model_dump(),
            "mode": "simulation_only",
            "live_available": False,
            "private_available": self.o.settings.app_env != "prod",
            "timezone": self.o.settings.clinic_timezone,
        }

    async def configure(
        self, tenant: str, actor: str, policy: NoticePolicy, revision: int, platform: bool = False
    ) -> dict[str, Any]:
        try:
            return await self._configure(tenant, actor, policy, revision, platform)
        except IntegrityError as exc:
            raise BillingError("notice policy changed; reload it") from exc

    async def _configure(
        self, tenant: str, actor: str, policy: NoticePolicy, revision: int, platform: bool = False
    ) -> dict[str, Any]:
        self.private_only()
        scope = "platform" if platform else "clinic"
        async with self.db.engine.begin() as conn:
            row = (
                (
                    await conn.execute(
                        select(notice_policies)
                        .where(
                            notice_policies.c.tenant == tenant,
                            notice_policies.c.scope == scope,
                        )
                        .with_for_update()
                    )
                )
                .mappings()
                .first()
            )
            if (row["revision"] if row else 0) != revision:
                raise BillingError("notice policy changed; reload it")
            if row:
                await conn.execute(
                    update(notice_policies)
                    .where(
                        notice_policies.c.tenant == tenant,
                        notice_policies.c.scope == scope,
                    )
                    .values(policy=policy.model_dump(), revision=revision + 1)
                )
            else:
                await conn.execute(
                    insert(notice_policies).values(
                        tenant=tenant,
                        scope=scope,
                        policy=policy.model_dump(),
                        revision=1,
                    )
                )
            await self.o.audit.record_in(
                conn, tenant, actor, "billing.notice_trial.configured", "billing/notice_trials"
            )
        return await self.configuration(tenant, platform)

    async def history(
        self, tenant: str, invoice: str, platform: bool = False
    ) -> list[dict[str, Any]]:
        await self.o.billing_ledger.statement(tenant, invoice, platform=platform)
        async with self.db.engine.connect() as conn:
            rows = (
                (
                    await conn.execute(
                        select(notice_trials)
                        .where(
                            notice_trials.c.tenant == tenant,
                            notice_trials.c.invoice_id == invoice,
                        )
                        .order_by(notice_trials.c.simulated_at, notice_trials.c.id)
                    )
                )
                .mappings()
                .all()
            )
        return [
            {
                "id": r["id"],
                "phase": r["phase"],
                "text": r["text"],
                "simulated_at": aware(r["simulated_at"]),
                "state": "simulated",
                "provider_message_id": None,
            }
            for r in rows
        ]

    async def simulate(
        self, tenant: str, invoice: str, actor: str, at: datetime, platform: bool = False
    ) -> dict[str, Any]:
        self.private_only()
        self.o.billing._enabled()
        if at.tzinfo is None:
            raise BillingError("trial date requires a timezone")
        normalized = aware(at)
        assert normalized is not None
        at = normalized
        context = await self.o.payment_followups._context(tenant, invoice, platform)
        owner = self.o.settings.billing_platform_tenant if platform else tenant
        config = await self.configuration(owner, platform)
        policy = NoticePolicy.model_validate(config["policy"])
        tz = ZoneInfo(self.o.settings.clinic_timezone)
        local = at.astimezone(tz)
        promise = aware(context["follow"].get("promised_for"))
        next_contact = aware(context["follow"].get("next_contact_at"))
        blocked = None
        result: dict[str, Any] = {
            "mode": "simulation_only",
            "sent": False,
            "provider_message_id": None,
            "at": at,
        }
        async with self.db.engine.begin() as conn:
            row = await self.o.billing._row(conn, tenant, invoice)
            if platform != (row["kind"] == "subscription"):
                raise KeyError("invoice scope not found")
            history = (
                (
                    await conn.execute(
                        select(notice_trials)
                        .where(
                            notice_trials.c.tenant == tenant,
                            notice_trials.c.invoice_id == invoice,
                        )
                        .order_by(notice_trials.c.simulated_at)
                    )
                )
                .mappings()
                .all()
            )
            keys = {h["event_key"] for h in history}
            remaining = row["amount_cents"] - row["credited_cents"]
            phase, key = "", ""
            if row["state"] == "paid":
                remaining, phase, key = 0, "paid", "paid"
            elif row["state"] not in {"pending", "rejected"}:
                blocked = "payment_needs_review"
            elif row["credited_cents"]:
                from orchestrator.billing_ledger import movements

                last = (
                    await conn.execute(
                        select(movements.c.id)
                        .where(
                            movements.c.tenant == tenant,
                            movements.c.invoice_id == invoice,
                        )
                        .order_by(movements.c.created_at.desc())
                        .limit(1)
                    )
                ).scalar_one_or_none()
                if last and "partial:" + last not in keys:
                    phase, key = "partial", "partial:" + last
            if not phase and not blocked:
                due = aware(row["due_at"])
                reminders = [h for h in history if h["phase"].startswith("post_")]
                last_reminder = aware(reminders[-1]["simulated_at"]) if reminders else None
                if due is None:
                    blocked = "deadline_missing"
                elif at < due:
                    if due - at <= timedelta(hours=policy.before_hours):
                        phase, key = "before_due", "before_due"
                    else:
                        blocked = "not_due"
                elif local.date() == due.astimezone(tz).date():
                    phase, key = "due", "due"
                elif last_reminder is not None and at - last_reminder < timedelta(
                    days=policy.cadence_days
                ):
                    blocked = "cadence_not_due"
                elif len(reminders) >= policy.maximum:
                    phase, key = "escalation", "escalation"
                else:
                    phase = "post_" + str(len(reminders) + 1)
                    key = phase
            if key in keys:
                blocked = "already_simulated"
            elif phase != "escalation" and not blocked:
                if not context["active"]:
                    blocked = "inactive_record"
                elif not context["phone"]:
                    blocked = "phone_missing"
                elif not context["allowed"]:
                    blocked = "permission_missing"
                elif context["opted_out"]:
                    blocked = "opted_out"
                elif context["follow"].get("paused"):
                    blocked = "paused"
                elif phase not in {"paid", "partial"} and promise is not None and promise > at:
                    blocked = "payment_promised"
                elif (
                    phase not in {"paid", "partial"}
                    and next_contact is not None
                    and next_contact > at
                ):
                    blocked = "contact_not_due"
                elif not policy.start_hour <= local.hour < policy.end_hour:
                    blocked = "outside_hours"
            if not blocked and phase:
                text = policy.texts[phase].format(
                    balance=_money(remaining),
                    amount=_money(row["amount_cents"]),
                    reference=invoice[:8],
                )
                identifier = uuid.uuid4().hex
                await conn.execute(
                    insert(notice_trials).values(
                        tenant=tenant,
                        id=identifier,
                        invoice_id=invoice,
                        event_key=key,
                        phase=phase,
                        text=text,
                        simulated_at=at,
                        created_at=utcnow(),
                    )
                )
                result.update(
                    state="simulated",
                    phase=phase,
                    text=text,
                    id=identifier,
                    audience="administrator" if phase == "escalation" else "payer",
                )
            else:
                result.update(state="blocked", reason=blocked or "no_action")
            await self.o.audit.record_in(
                conn,
                tenant,
                actor,
                "billing.notice_trial." + result["state"],
                f"billing/{invoice}",
                subject_id=row["patient_id"],
                details={
                    "correlation_id": invoice,
                    "phase": phase,
                    "reason": blocked,
                    "simulated": True,
                    "simulated_at": at.isoformat(),
                },
            )
        return result
