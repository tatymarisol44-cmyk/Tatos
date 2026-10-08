"""Appointment reminders: the practical answer to the no-show risk of follow-up.

About a day before each booked visit (`REMINDER_HOURS_BEFORE`, 24) the patient gets one
short reminder: the practice, the day and the time, nothing clinical. A care message, not
marketing: it does not need the marketing consent, but a WhatsApp STOP is respected.

Channel, in order: Telegram when the patient linked it; WhatsApp with the approved
utility template `WHATSAPP_REMINDER_TEMPLATE` (outside the 24-hour window only templates
may be sent) when the practice has an active WhatsApp account and the patient a phone;
otherwise the reminder is the visit in the patient's app. Without credentials: dry run.

Each visit is claimed with a conditional UPDATE before anything is sent, so the background
worker can run on every replica and still send one reminder per visit. The audit records
the outcome, never the text or the number.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import and_, select, update

from orchestrator.campaigns import DeliveryUncertain, TelegramChannel
from orchestrator.config import Settings
from orchestrator.crm import ACTIVE_STATUSES, appointments, patients
from orchestrator.db import Database, aware, utcnow
from orchestrator.governance import AuditLog
from orchestrator.publishers import PublishError, WhatsAppSender
from orchestrator.social import channel_accounts, resolve_secret

log = logging.getLogger(__name__)
SYSTEM_ACTOR = "system:reminders"


def reminder_text(first_name: str, practice: str, when_local: str) -> str:
    return (
        f"Hola {first_name} 👋 Te recordamos tu cita en {practice} el {when_local}. "
        "Si no puedes asistir, respóndenos para reprogramarla. ¡Te esperamos! 💙"
    )


class Reminders:
    def __init__(
        self,
        db: Database,
        audit: AuditLog,
        settings: Settings,
        telegram: TelegramChannel,
        is_opted_out: Any,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.db = db
        self.audit = audit
        self.settings = settings
        self.telegram = telegram
        self.is_opted_out = is_opted_out  # InboundService.is_opted_out (WhatsApp STOP)
        self.transport = transport
        self.tz = ZoneInfo(settings.clinic_timezone)

    async def due(self, now: datetime | None = None) -> int:
        """Send every reminder now due; returns how many visits were handled."""
        now = now or utcnow()
        horizon = now + timedelta(hours=self.settings.reminder_hours_before)
        query = select(appointments.c.tenant, appointments.c.id).where(
            and_(
                appointments.c.status.in_(ACTIVE_STATUSES),
                appointments.c.reminded_at.is_(None),
                appointments.c.starts_at > now + timedelta(hours=1),
                appointments.c.starts_at <= horizon,
            )
        )
        async with self.db.engine.connect() as conn:
            rows = list(await conn.execute(query))
        handled = 0
        for tenant, appointment_id in rows:
            if await self._claim(tenant, appointment_id, now):
                await self._remind(tenant, appointment_id)
                handled += 1
        return handled

    async def _claim(self, tenant: str, appointment_id: str, now: datetime) -> bool:
        async with self.db.engine.begin() as conn:
            done = await conn.execute(
                update(appointments)
                .where(
                    and_(
                        appointments.c.tenant == tenant,
                        appointments.c.id == appointment_id,
                        appointments.c.reminded_at.is_(None),
                    )
                )
                .values(reminded_at=now)
            )
            return bool(done.rowcount == 1)

    async def _remind(self, tenant: str, appointment_id: str) -> None:
        query = (
            select(
                appointments.c.starts_at,
                appointments.c.patient_id,
                patients.c.display_name,
                patients.c.phone,
                patients.c.telegram_chat_id,
                patients.c.restricted,
            )
            .join(
                patients,
                and_(
                    patients.c.tenant == appointments.c.tenant,
                    patients.c.id == appointments.c.patient_id,
                ),
            )
            .where(and_(appointments.c.tenant == tenant, appointments.c.id == appointment_id))
        )
        async with self.db.engine.connect() as conn:
            row = (await conn.execute(query)).first()
        if row is None or row.restricted:
            return
        start = aware(row.starts_at)
        assert start is not None
        when = start.astimezone(self.tz).strftime("%d/%m a las %H:%M")
        first = row.display_name.split()[0]
        channel, outcome = await self._deliver(tenant, row, first, when)
        await self.audit.record(
            tenant,
            SYSTEM_ACTOR,
            f"appointment.reminder_{outcome}",
            f"appointment/{appointment_id}",
            subject_id=row.patient_id,
            details={"channel": channel},
        )

    async def _deliver(self, tenant: str, row: Any, first: str, when: str) -> tuple[str, str]:
        practice = self.settings.practice_display_name
        if row.telegram_chat_id:
            try:
                return "telegram", await self.telegram.send(
                    row.telegram_chat_id, reminder_text(first, practice, when)
                )
            except DeliveryUncertain:
                return "telegram", "uncertain"
            except RuntimeError:
                return "telegram", "failed"
        template = self.settings.whatsapp_reminder_template
        if row.phone and template:
            account = await self._whatsapp_account(tenant)
            if account is not None:
                if await self.is_opted_out(tenant, "whatsapp", row.phone):
                    return "whatsapp", "opted_out"
                token = resolve_secret(account.secret_ref)
                if token is None:
                    return "whatsapp", "dry_run"
                try:
                    async with httpx.AsyncClient(
                        timeout=self.settings.publish_timeout_seconds, transport=self.transport
                    ) as client:
                        await WhatsAppSender(
                            client, self.settings.whatsapp_graph_base
                        ).send_template(
                            phone_number_id=account.external_id,
                            token=token,
                            to="".join(ch for ch in row.phone if ch.isdigit()),
                            name=template,
                            language=self.settings.whatsapp_alert_template_language,
                            parameters=[first, practice, when],
                        )
                    return "whatsapp", "sent"
                except PublishError:
                    return "whatsapp", "failed"
                except httpx.TransportError:
                    return "whatsapp", "uncertain"
        return "app", "in_app"  # the visit is in the patient's app

    async def _whatsapp_account(self, tenant: str) -> Any:
        query = (
            select(channel_accounts.c.external_id, channel_accounts.c.secret_ref)
            .where(
                and_(
                    channel_accounts.c.tenant == tenant,
                    channel_accounts.c.network == "whatsapp",
                    channel_accounts.c.active.is_(True),
                )
            )
            .limit(1)
        )
        async with self.db.engine.connect() as conn:
            return (await conn.execute(query)).first()
