"""A test answered by the patient in their app (SRS RF-TST-03).

The patient sees the questions and, after answering, a thank-you: never the score or the
band, which the professional interprets. When an answer trips an alert rule (PHQ-9 item 9,
thoughts of self-harm), the same moment matters as for a crisis message:

* the patient gets the warm text with the emergency lines at once (auto_reply.py, the
  same words as on WhatsApp);
* a crisis alert opens for the team, naming the patient, and the on-call person is
  notified now, not at the next worker pass (ADR 0017).

The model is never involved: the answers are health data (classification.py)."""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import and_, select

from orchestrator.auto_reply import DEFAULT_CRISIS_HELP, reply_text
from orchestrator.config import Settings
from orchestrator.crm import patients
from orchestrator.db import Database
from orchestrator.inbound import InboundService
from orchestrator.instruments import Instruments
from orchestrator.oncall import OnCall

log = logging.getLogger(__name__)

THANKS = (
    "¡Gracias! Tus respuestas llegaron a tu profesional, que las revisará contigo. "
    "No hay respuestas buenas ni malas."
)


class SelfReports:
    def __init__(
        self,
        db: Database,
        instruments: Instruments,
        inbound: InboundService,
        oncall: OnCall,
        settings: Settings,
    ) -> None:
        self.db = db
        self.instruments = instruments
        self.inbound = inbound
        self.oncall = oncall
        self.settings = settings

    async def pending(self, tenant: str, patient_id: str) -> list[dict[str, Any]]:
        return await self.instruments.pending_for(tenant, patient_id)

    async def submit(
        self,
        tenant: str,
        patient_id: str,
        actor: str,
        assignment_id: str,
        answers: dict[str, Any],
    ) -> dict[str, Any]:
        """Raises KeyError (not theirs, or already answered) or InstrumentError."""
        outcome = await self.instruments.submit(tenant, patient_id, actor, assignment_id, answers)
        if not outcome["alerts"]:
            return {"status": "received", "message": THANKS}
        await self.inbound.open_alert(
            tenant, "crisis", "app", "", await self._phone(tenant, patient_id), patient_id
        )
        # No personal data in logs: the alert itself names the patient for the team.
        log.warning("crisis alert opened from a test answered in the app (tenant %s)", tenant)
        try:
            await self.oncall.escalate_due()
        except Exception:  # the worker on any replica retries a due step
            log.exception("on-call notice failed; the worker will retry")
        crisis = reply_text(
            "crisis",
            self.settings.practice_display_name,
            self.settings.crisis_help_text or DEFAULT_CRISIS_HELP,
        )
        return {"status": "received", "message": THANKS, "help": crisis}

    async def _phone(self, tenant: str, patient_id: str) -> str:
        """The number to call back, when the record has one."""
        query = select(patients.c.phone).where(
            and_(patients.c.tenant == tenant, patients.c.id == patient_id)
        )
        async with self.db.engine.connect() as conn:
            phone = (await conn.execute(query)).scalar_one_or_none()
        return phone or ""
