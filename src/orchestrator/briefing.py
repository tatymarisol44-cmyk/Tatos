"""The summary before a session (SRS RF-DOC-03): what the professional reads in the minute
before the patient comes in, prepared automatically and always marked as a draft.

* **Prepared by code, not by a model.** Health data may not reach a model
  (classification.py: HEALTH excludes "models"), so the summary is composed from the
  follow-up figures (followup.py), the record's index and the inbox, never from the text of
  a note. Every line can be traced to its source, and nothing depends on a provider.
* **A draft that suggests where to look.** Bands and risks are aids; the professional
  interprets them. Nothing is stored: it is computed on request.
* **Need to know.** Clinicians only; it reads the record the way the professional may (a
  psychotherapy note is counted for its author only), and preparing it is audited."""

from __future__ import annotations

import re
from collections import Counter
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from orchestrator.auth import Principal
from orchestrator.clinical_records import ClinicalRecords
from orchestrator.config import Settings
from orchestrator.followup import FollowUp
from orchestrator.governance import AuditLog
from orchestrator.inbox import InboxService
from orchestrator.instruments import Instruments

RISK = {"high": "alto", "medium": "medio", "low": "bajo"}
DIRECTION = {"worse": "empeoró", "better": "mejoró", "same": "sin cambio de banda"}
NETWORK = {"whatsapp": "WhatsApp", "instagram": "Instagram", "facebook": "Facebook"}
# The scales' bands (scales.py), in the words the professional uses.
BANDS = {
    "minimal": "mínima",
    "mild": "leve",
    "moderate": "moderada",
    "moderately_severe": "moderadamente grave",
    "severe": "grave",
}


def band_es(band: str | None) -> str:
    return BANDS.get(band or "", band or "sin banda")


# followup.no_show_risk explains itself in English for the API; here, in Spanish.
REASONS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"^missed (\d+) of (\d+) visits$"), "faltó a {0} de {1} citas"),
    (re.compile(r"^missed the last visit$"), "faltó a la última cita"),
    (re.compile(r"^cancelled (\d+) visits$"), "canceló {0} citas"),
    (re.compile(r"^no follow-up visit booked$"), "no tiene cita de seguimiento agendada"),
)


def reason_es(reason: str) -> str:
    for pattern, template in REASONS:
        if match := pattern.match(reason):
            return template.format(*match.groups())
    return reason  # a reason added later shows as written rather than disappearing


def compose(
    facts: dict[str, Any],
    documents: list[dict[str, Any]],
    conversations: list[dict[str, Any]],
    tz: ZoneInfo,
    now: datetime,
    assignments: list[dict[str, Any]] | None = None,
) -> list[str]:
    """The summary's lines, in Spanish. Pure: tested without a database."""

    def when(value: str | datetime | None) -> str:
        if value is None:
            return ""
        moment = datetime.fromisoformat(value) if isinstance(value, str) else value
        return moment.astimezone(tz).strftime("%d/%m/%Y %H:%M")

    lines = [f"Resumen previo a la sesión: {facts['name']} (borrador automático)"]
    visits = facts["attendance"]
    if visits["next_visit"]:
        lines.append(f"Próxima cita: {when(visits['next_visit'])}.")
    else:
        lines.append("No tiene una próxima cita agendada.")
    if visits["last_visit"]:
        days = visits["days_since_last_visit"]
        lines.append(f"Última sesión: {when(visits['last_visit'])} (hace {days} días).")
    else:
        lines.append("Aún no hay sesiones completadas.")
    decided = visits["completed"] + visits["no_shows"]
    if decided:
        rate = round(100 * visits["completed"] / decided)
        lines.append(
            f"Asistencia: {visits['completed']} de {decided} citas ({rate} %); "
            f"{visits['cancelled']} canceladas."
        )
    risk = facts["no_show_risk"]
    if risk["level"] != "low":
        reasons = "; ".join(reason_es(r) for r in risk["reasons"])
        lines.append(f"Riesgo de inasistencia {RISK[risk['level']]}: {reasons}.")
    for test in facts["tests"]:
        last = test["last"]
        line = f"{test['instrument']}: {last['total']} ({band_es(last['band'])})"
        if test["applied"] > 1:
            first = test["first"]
            line += (
                f", antes {first['total']} ({band_es(first['band'])}): "
                f"{DIRECTION[test['direction']]}"
            )
        lines.append(line + f" [{test['applied']} aplicaciones].")
        for alert in test["alerts_last"]:
            lines.append(f"Atención en {test['instrument']}: {alert}")
    for sent in assignments or []:
        if sent["status"] == "pending":
            lines.append(
                f"Test enviado sin responder: {sent['name']} (desde {when(sent['assigned_at'])})."
            )
    if documents:
        kinds = Counter(d["doc_type"] for d in documents)
        listed = ", ".join(f"{n} {kind}" for kind, n in sorted(kinds.items()))
        latest = max(documents, key=lambda d: d["created_at"])
        lines.append(
            f"Historia: {len(documents)} entradas ({listed}); la última, "
            f"{latest['doc_type']} del {when(latest['created_at'])}."
        )
    else:
        lines.append("Historia: sin entradas todavía.")
    for conversation in conversations:
        network = NETWORK.get(conversation["network"], conversation["network"])
        notes = []
        if conversation["unread"]:
            notes.append(f"{conversation['unread']} sin leer")
        if conversation["attention"] == "crisis":
            notes.append("marcada como crisis")
        elif conversation["attention"] == "human":
            notes.append("pidió hablar con una persona")
        if notes:
            lines.append(f"Mensajes en {network}: {', '.join(notes)}.")
    lines.append(
        "Las bandas de los tests son una ayuda, no un diagnóstico; revisa la historia antes "
        "de decidir."
    )
    return lines


class Briefing:
    def __init__(
        self,
        followup: FollowUp,
        clinical: ClinicalRecords,
        instruments: Instruments,
        inbox: InboxService,
        audit: AuditLog,
        settings: Settings,
    ) -> None:
        self.followup = followup
        self.clinical = clinical
        self.instruments = instruments
        self.inbox = inbox
        self.audit = audit
        self.tz = ZoneInfo(settings.clinic_timezone)

    async def prepare(
        self, principal: Principal, patient_id: str, now: datetime | None = None
    ) -> dict[str, Any]:
        """Raises KeyError for a patient this tenant does not have."""
        now = now or datetime.now(UTC)
        facts = await self.followup.patient(principal.tenant, patient_id, now)
        documents = await self.clinical.list(principal, patient_id)
        conversations = await self.inbox.for_patient(principal.tenant, patient_id)
        assignments = await self.instruments.assignments(principal, patient_id)
        lines = compose(facts, documents, conversations, self.tz, now, assignments)
        await self.audit.record(
            principal.tenant,
            principal.id,
            "briefing.prepared",
            f"patient/{patient_id}",
            subject_id=patient_id,
        )
        return {
            "patient_id": patient_id,
            "draft": True,
            "prepared_by": "system",
            "prepared_at": now.isoformat(),
            "lines": lines,
            "text": "\n".join(lines),
            "flags": facts["flags"],
        }
