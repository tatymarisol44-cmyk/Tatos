"""Telling the on-call person about an alert, and escalating when nobody takes it (ADR 0017).

* **Levels.** Each establishment lists its on-call contacts by level: 1 is whoever is on
  duty, 2 the backup, and so on. Each contact may have Telegram, WhatsApp and e-mail; a
  notice goes out on every channel the contact has (the owner chose all three).
* **Escalation.** A new alert notifies level 1 at once. A crisis alert nobody acknowledges
  within ALERT_ESCALATION_MINUTES goes to the next level, and so on up the list; a request
  to talk to a person notifies level 1 only. Acknowledging or resolving stops it.
* **Once, on any number of replicas.** Each step is claimed with a conditional UPDATE on
  (level, due time) before anything is sent, so two replicas never notify twice, and a
  replica that dies mid-way leaves the next step due for another one.
* **No patient data in a notice:** the kind of alert, a reference and the console address.
  Not the message (it is never stored), not the caller's number (it is in the console,
  where reading it is audited). Every notice and its outcome is recorded."""

from __future__ import annotations

import asyncio
import logging
import re
import smtplib
import ssl
import uuid
from datetime import datetime, timedelta
from email.message import EmailMessage
from typing import Any, Literal

import httpx
from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Integer,
    String,
    Table,
    and_,
    insert,
    select,
    update,
)

from orchestrator.campaigns import DeliveryUncertain, TelegramChannel
from orchestrator.config import Settings
from orchestrator.db import Database, metadata, utcnow
from orchestrator.governance import AuditLog
from orchestrator.inbound import channel_alerts
from orchestrator.publishers import PublishError, WhatsAppSender
from orchestrator.social import channel_accounts, resolve_secret

log = logging.getLogger(__name__)

Channel = Literal["telegram", "whatsapp", "email"]
MAX_LEVEL = 5
SYSTEM_ACTOR = "system:on-call"
EMAIL = re.compile(r"^[^@\s]{1,64}@[A-Za-z0-9.-]{1,190}\.[A-Za-z]{2,}$")

on_call_contacts = Table(
    "on_call_contacts",
    metadata,
    Column("tenant", String(64), primary_key=True),
    Column("contact_id", String(32), primary_key=True),
    Column("display_name", String(120), nullable=False),
    Column("level", Integer, nullable=False),
    Column("telegram_chat_id", String(32), nullable=True),
    Column("whatsapp_number", String(20), nullable=True),
    Column("email", String(254), nullable=True),
    Column("active", Boolean, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("created_by", String(128), nullable=False),
)

alert_notifications = Table(
    "alert_notifications",
    metadata,
    Column("tenant", String(64), primary_key=True),
    Column("notification_id", String(32), primary_key=True),
    Column("alert_id", String(32), nullable=False, index=True),
    Column("level", Integer, nullable=False),
    Column("contact_id", String(32), nullable=True),  # null: nobody at that level
    Column("channel", String(16), nullable=False),
    Column("outcome", String(16), nullable=False),  # sent|dry_run|skipped|failed|uncertain|none
    Column("detail", String(160), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
)


class OnCallError(ValueError):
    """A contact with no channel, a bad e-mail or number, or a level out of range."""


def notice_text(kind: str, alert_id: str, console_url: str) -> str:
    what = "CRISIS" if kind == "crisis" else "solicitud de hablar con una persona"
    return (
        f"Alerta de {what} abierta. Referencia: {alert_id}. "
        f"Atiéndela ahora en el sistema: {console_url} . "
        "No respondas este mensaje: aquí no hay datos del paciente."
    )


class Notifier:
    """The three channels. Each returns an outcome and never raises."""

    def __init__(
        self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self.settings = settings
        self.transport = transport
        self.telegram = TelegramChannel(settings)

    async def telegram_notice(self, chat_id: str, text: str) -> tuple[str, str | None]:
        try:
            return await self.telegram.send(chat_id, text), None
        except DeliveryUncertain as exc:
            return "uncertain", str(exc)
        except RuntimeError as exc:
            return "failed", str(exc)

    async def whatsapp_notice(
        self, tenant: str, db: Database, number: str, alert_id: str
    ) -> tuple[str, str | None]:
        template = self.settings.whatsapp_alert_template
        if template is None:
            return "skipped", "no approved WhatsApp template configured"
        query = (
            select(channel_accounts.c.external_id, channel_accounts.c.secret_ref)
            .where(
                and_(
                    channel_accounts.c.tenant == tenant,
                    channel_accounts.c.network == "whatsapp",
                    channel_accounts.c.active.is_(True),
                )
            )
            .order_by(channel_accounts.c.created_at)
            .limit(1)
        )
        async with db.engine.connect() as conn:
            account = (await conn.execute(query)).first()
        if account is None:
            return "skipped", "no WhatsApp account connected"
        token = resolve_secret(account.secret_ref)
        if token is None:
            return "dry_run", None
        try:
            async with httpx.AsyncClient(
                timeout=self.settings.publish_timeout_seconds, transport=self.transport
            ) as client:
                await WhatsAppSender(client, self.settings.whatsapp_graph_base).send_template(
                    phone_number_id=account.external_id,
                    token=token,
                    to=number,
                    name=template,
                    language=self.settings.whatsapp_alert_template_language,
                    parameters=[alert_id],
                )
        except PublishError as exc:
            return "failed", str(exc)
        except httpx.TransportError as exc:
            return "uncertain", f"transport: {type(exc).__name__}"
        return "sent", None

    def _send_mail(self, to: str, subject: str, text: str) -> None:
        s = self.settings
        message = EmailMessage()
        message["From"] = s.smtp_from or s.smtp_username or "no-reply@localhost"
        message["To"] = to
        message["Subject"] = subject
        message.set_content(text)
        with smtplib.SMTP(s.smtp_host or "", s.smtp_port, timeout=s.smtp_timeout_seconds) as smtp:
            smtp.starttls(context=ssl.create_default_context())  # never in clear text
            if s.smtp_username and s.smtp_password:
                smtp.login(s.smtp_username, s.smtp_password.get_secret_value())
            smtp.send_message(message)

    async def email_notice(self, to: str, subject: str, text: str) -> tuple[str, str | None]:
        if not self.settings.smtp_host:
            return "dry_run", None
        try:
            await asyncio.to_thread(self._send_mail, to, subject, text)
        except (smtplib.SMTPException, OSError) as exc:
            return "failed", type(exc).__name__  # never the server's text: it may echo data
        return "sent", None


class OnCall:
    def __init__(
        self, db: Database, audit: AuditLog, settings: Settings, notifier: Notifier | None = None
    ) -> None:
        self.db = db
        self.audit = audit
        self.settings = settings
        self.notifier = notifier or Notifier(settings)

    # --- contacts ---------------------------------------------------------------------

    async def add_contact(
        self,
        tenant: str,
        actor: str,
        *,
        display_name: str,
        level: int,
        telegram_chat_id: str | None = None,
        whatsapp_number: str | None = None,
        email: str | None = None,
    ) -> dict[str, Any]:
        if not 1 <= level <= MAX_LEVEL:
            raise OnCallError(f"level must be 1 to {MAX_LEVEL}")
        number = re.sub(r"\D", "", whatsapp_number) if whatsapp_number else None
        if number is not None and not 8 <= len(number) <= 15:
            raise OnCallError("the WhatsApp number needs its country code (8 to 15 digits)")
        if email is not None and not EMAIL.fullmatch(email):
            raise OnCallError("invalid e-mail address")
        if not (telegram_chat_id or number or email):
            raise OnCallError("a contact needs at least one channel")
        contact_id = uuid.uuid4().hex[:12]
        async with self.db.engine.begin() as conn:
            await conn.execute(
                insert(on_call_contacts).values(
                    tenant=tenant,
                    contact_id=contact_id,
                    display_name=display_name,
                    level=level,
                    telegram_chat_id=telegram_chat_id,
                    whatsapp_number=number,
                    email=email,
                    active=True,
                    created_at=utcnow(),
                    created_by=actor,
                )
            )
            await self.audit.record_in(
                conn,
                tenant,
                actor,
                "on_call.contact_added",
                f"on_call_contact/{contact_id}",
                details={"level": level},
            )
        return (await self.contacts(tenant, contact_id=contact_id))[0]

    async def contacts(
        self, tenant: str, *, level: int | None = None, contact_id: str | None = None
    ) -> list[dict[str, Any]]:
        query = select(on_call_contacts).where(on_call_contacts.c.tenant == tenant)
        if level is not None:
            query = query.where(
                and_(on_call_contacts.c.level == level, on_call_contacts.c.active.is_(True))
            )
        if contact_id is not None:
            query = query.where(on_call_contacts.c.contact_id == contact_id)
        query = query.order_by(on_call_contacts.c.level, on_call_contacts.c.created_at)
        async with self.db.engine.connect() as conn:
            return [
                {c.name: getattr(r, c.name) for c in on_call_contacts.c if c.name != "tenant"}
                for r in await conn.execute(query)
            ]

    async def disable_contact(self, tenant: str, contact_id: str, actor: str) -> bool:
        query = (
            update(on_call_contacts)
            .where(
                and_(
                    on_call_contacts.c.tenant == tenant,
                    on_call_contacts.c.contact_id == contact_id,
                    on_call_contacts.c.active.is_(True),
                )
            )
            .values(active=False)
        )
        async with self.db.engine.begin() as conn:
            done = (await conn.execute(query)).rowcount == 1
            if done:
                await self.audit.record_in(
                    conn, tenant, actor, "on_call.contact_disabled", f"on_call_contact/{contact_id}"
                )
        return done

    # --- alerts -----------------------------------------------------------------------

    async def acknowledge(self, tenant: str, alert_id: str, actor: str) -> bool:
        """Someone took the alert: stop calling others. It stays open until resolved."""
        query = (
            update(channel_alerts)
            .where(
                and_(
                    channel_alerts.c.tenant == tenant,
                    channel_alerts.c.alert_id == alert_id,
                    channel_alerts.c.status == "open",
                    channel_alerts.c.acknowledged_at.is_(None),
                )
            )
            .values(acknowledged_by=actor, acknowledged_at=utcnow(), next_escalation_at=None)
        )
        async with self.db.engine.begin() as conn:
            done = (await conn.execute(query)).rowcount == 1
            if done:
                await self.audit.record_in(
                    conn, tenant, actor, "channel_alert.acknowledged", f"channel_alert/{alert_id}"
                )
        return done

    async def notifications(self, tenant: str, alert_id: str) -> list[dict[str, Any]]:
        query = (
            select(alert_notifications)
            .where(
                and_(
                    alert_notifications.c.tenant == tenant,
                    alert_notifications.c.alert_id == alert_id,
                )
            )
            .order_by(alert_notifications.c.created_at, alert_notifications.c.notification_id)
        )
        async with self.db.engine.connect() as conn:
            return [
                {c.name: getattr(r, c.name) for c in alert_notifications.c if c.name != "tenant"}
                for r in await conn.execute(query)
            ]

    async def escalate_due(self, now: datetime | None = None) -> int:
        """Notify the next level of every alert that is due. Returns the steps taken."""
        now = now or utcnow()
        due = select(
            channel_alerts.c.tenant,
            channel_alerts.c.alert_id,
            channel_alerts.c.kind,
            channel_alerts.c.escalation_level,
            channel_alerts.c.next_escalation_at,
        ).where(
            and_(
                channel_alerts.c.status == "open",
                channel_alerts.c.acknowledged_at.is_(None),
                channel_alerts.c.next_escalation_at.is_not(None),
                channel_alerts.c.next_escalation_at <= now,
            )
        )
        async with self.db.engine.connect() as conn:
            rows = (await conn.execute(due)).all()
        steps = 0
        for row in rows:
            if await self._step(row, now):
                steps += 1
        return steps

    async def _step(self, row: Any, now: datetime) -> bool:
        level = row.escalation_level + 1
        contacts = await self.contacts(row.tenant, level=level) if level <= MAX_LEVEL else []
        more = row.kind == "crisis" and level < MAX_LEVEL
        if contacts:
            wait = timedelta(minutes=self.settings.alert_escalation_minutes)
        else:
            wait = timedelta(0)  # nobody at this level: try the next one at once
        next_due = now + wait if more else None
        # Claim: only the replica whose UPDATE matches the state it read takes this step.
        claim = (
            update(channel_alerts)
            .where(
                and_(
                    channel_alerts.c.tenant == row.tenant,
                    channel_alerts.c.alert_id == row.alert_id,
                    channel_alerts.c.status == "open",
                    channel_alerts.c.acknowledged_at.is_(None),
                    channel_alerts.c.escalation_level == row.escalation_level,
                    channel_alerts.c.next_escalation_at == row.next_escalation_at,
                )
            )
            .values(escalation_level=level, next_escalation_at=next_due)
        )
        async with self.db.engine.begin() as conn:
            if (await conn.execute(claim)).rowcount != 1:
                return False
        if not contacts:
            await self._record(
                row.tenant, row.alert_id, level, None, "none", "none", "nobody at this level"
            )
            if not more:
                log.warning(
                    "on-call: alert %s reached the end of the list unattended", row.alert_id
                )
                await self.audit.record(
                    row.tenant,
                    SYSTEM_ACTOR,
                    "channel_alert.unattended",
                    f"channel_alert/{row.alert_id}",
                    details={"level": level},
                )
            return True
        await self._notify(row, level, contacts)
        return True

    async def _notify(self, row: Any, level: int, contacts: list[dict[str, Any]]) -> None:
        console = self.settings.public_base_url.rstrip("/") + "/"
        text = notice_text(row.kind, row.alert_id, console)
        subject = "ALERTA DE CRISIS" if row.kind == "crisis" else "Alerta: hablar con una persona"
        for contact in contacts:
            jobs: list[tuple[Channel, Any]] = []
            if contact["telegram_chat_id"]:
                jobs.append(
                    ("telegram", self.notifier.telegram_notice(contact["telegram_chat_id"], text))
                )
            if contact["whatsapp_number"]:
                jobs.append(
                    (
                        "whatsapp",
                        self.notifier.whatsapp_notice(
                            row.tenant, self.db, contact["whatsapp_number"], row.alert_id
                        ),
                    )
                )
            if contact["email"]:
                jobs.append(("email", self.notifier.email_notice(contact["email"], subject, text)))
            # All channels at once: a crisis should not wait for a slow mail server.
            results = await asyncio.gather(*(job for _, job in jobs))
            for (channel, _), (outcome, detail) in zip(jobs, results, strict=True):
                await self._record(
                    row.tenant, row.alert_id, level, contact["contact_id"], channel, outcome, detail
                )
        await self.audit.record(
            row.tenant,
            SYSTEM_ACTOR,
            "channel_alert.notified",
            f"channel_alert/{row.alert_id}",
            details={"level": level, "contacts": len(contacts)},
        )

    async def _record(
        self,
        tenant: str,
        alert_id: str,
        level: int,
        contact_id: str | None,
        channel: str,
        outcome: str,
        detail: str | None,
    ) -> None:
        async with self.db.engine.begin() as conn:
            await conn.execute(
                insert(alert_notifications).values(
                    tenant=tenant,
                    notification_id=uuid.uuid4().hex[:16],
                    alert_id=alert_id,
                    level=level,
                    contact_id=contact_id,
                    channel=channel,
                    outcome=outcome,
                    detail=(detail or None) and detail[:160],
                    created_at=utcnow(),
                )
            )

    async def close(self) -> None:
        await self.notifier.telegram.close()
