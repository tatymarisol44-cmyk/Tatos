"""The engagement index (SRS RF-MKT-01), in the three dimensions of customer engagement.

Vivek et al. (2014) and Vera-Martínez & Ornelas-Sánchez (2021) measure engagement with a
12-item questionnaire in three dimensions; engagement predicts loyalty and satisfaction.
The practice does not ask its patients that questionnaire: this index reads the same
three dimensions from what the platform already sees, over the last 180 days, so it is a
behavioural *proxy* of the scale, labelled as such:

* **Atención consciente** (conscious attention): of the practice's messages delivered to
  the patient's app, the share she opened.
* **Participación entusiasta** (enthused participation): visits attended of those booked,
  and tests answered of those sent.
* **Conexión social** (social connection): workshops attended, and coming with company.

Each dimension is 0-100, or absent when there is nothing to measure (no message sent, no
visit, no workshop): an absent dimension is never counted as a zero.

Rules (classification.py, LOPDP / GDPR Art. 22):
* only for patients who granted the **analytics** consent: profiling without it is not
  computed at all;
* an indicator for the practice ("insights"), never a campaign segment: it reads visits
  and tests, which are health data and never reach campaigns or advertising."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import timedelta
from statistics import fmean
from typing import Any

from sqlalchemy import and_, func, select

from orchestrator.campaigns import recipients
from orchestrator.crm import appointments, patients
from orchestrator.db import Database, utcnow
from orchestrator.governance import ConsentRegistry, Purpose
from orchestrator.instruments import instrument_assignments
from orchestrator.workshops import workshop_registrations

WINDOW = timedelta(days=180)
HIGH, MEDIUM = 70, 40


def level(score: float | None) -> str | None:
    if score is None:
        return None
    return "alto" if score >= HIGH else "medio" if score >= MEDIUM else "bajo"


def social_score(attended: int, registered: int, with_company: bool) -> int | None:
    """Workshops: one attended 60, two 80, three or more 100; company adds 20; signing up
    without coming counts 20. None when the patient never signed up for one."""
    if not registered:
        return None
    base = {0: 20, 1: 60, 2: 80}.get(attended, 100)
    return min(100, base + (20 if with_company and attended else 0))


def overall(dimensions: Mapping[str, float | None]) -> float | None:
    present = [v for v in dimensions.values() if v is not None]
    return round(fmean(present)) if present else None


class Engagement:
    def __init__(self, db: Database, consents: ConsentRegistry) -> None:
        self.db = db
        self.consents = consents

    async def for_patient(self, tenant: str, patient_id: str) -> dict[str, Any]:
        if not await self.consents.has(tenant, patient_id, Purpose.ANALYTICS):
            return {"available": False, "reason": "sin consentimiento de analítica"}
        since = utcnow() - WINDOW
        async with self.db.engine.connect() as conn:
            delivered, seen = (
                await conn.execute(
                    select(
                        func.count().filter(recipients.c.status.in_(("in_app", "seen", "sent"))),
                        func.count().filter(recipients.c.seen_at.is_not(None)),
                    ).where(
                        and_(
                            recipients.c.tenant == tenant,
                            recipients.c.patient_id == patient_id,
                            recipients.c.arm == "treatment",
                            func.coalesce(recipients.c.sent_at, recipients.c.seen_at) >= since,
                        )
                    )
                )
            ).one()
            completed, missed = (
                await conn.execute(
                    select(
                        func.count().filter(appointments.c.status == "completed"),
                        func.count().filter(appointments.c.status == "no_show"),
                    ).where(
                        and_(
                            appointments.c.tenant == tenant,
                            appointments.c.patient_id == patient_id,
                            appointments.c.starts_at >= since,
                        )
                    )
                )
            ).one()
            answered, sent = (
                await conn.execute(
                    select(
                        func.count().filter(instrument_assignments.c.status == "completed"),
                        func.count().filter(instrument_assignments.c.status != "cancelled"),
                    ).where(
                        and_(
                            instrument_assignments.c.tenant == tenant,
                            instrument_assignments.c.patient_id == patient_id,
                            instrument_assignments.c.assigned_at >= since,
                        )
                    )
                )
            ).one()
            attended, registered, company = (
                await conn.execute(
                    select(
                        func.count().filter(workshop_registrations.c.status == "attended"),
                        func.count().filter(workshop_registrations.c.status != "withdrawn"),
                        func.count().filter(
                            and_(
                                workshop_registrations.c.status == "attended",
                                workshop_registrations.c.companions > 0,
                            )
                        ),
                    ).where(
                        and_(
                            workshop_registrations.c.tenant == tenant,
                            workshop_registrations.c.patient_id == patient_id,
                            workshop_registrations.c.created_at >= since,
                        )
                    )
                )
            ).one()
        participation = [
            100 * completed / (completed + missed) if completed + missed else None,
            100 * answered / sent if sent else None,
        ]
        present = [v for v in participation if v is not None]
        dimensions: dict[str, float | None] = {
            "atencion": round(100 * seen / delivered) if delivered else None,
            "participacion": round(fmean(present)) if present else None,
            "conexion": social_score(int(attended), int(registered), bool(company)),
        }
        score = overall(dimensions)
        return {
            "available": True,
            "dimensions": dimensions,
            "score": score,
            "level": level(score),
            "basis": {
                "messages_opened": f"{seen} de {delivered}",
                "visits_attended": f"{completed} de {completed + missed}",
                "tests_answered": f"{answered} de {sent}",
                "workshops_attended": int(attended),
            },
        }

    async def summary(self, tenant: str) -> dict[str, Any]:
        """The practice's picture: how many consented, and how they spread by level."""
        query = select(patients.c.id).where(
            and_(patients.c.tenant == tenant, patients.c.restricted.is_(False))
        )
        async with self.db.engine.connect() as conn:
            ids = [r.id for r in await conn.execute(query)]
        levels = {"alto": 0, "medio": 0, "bajo": 0, "sin_datos": 0}
        sums: dict[str, list[float]] = {"atencion": [], "participacion": [], "conexion": []}
        consented = 0
        for pid in ids:
            view = await self.for_patient(tenant, pid)
            if not view["available"]:
                continue
            consented += 1
            levels[view["level"] or "sin_datos"] += 1
            for key, value in view["dimensions"].items():
                if value is not None:
                    sums[key].append(value)
        return {
            "patients": len(ids),
            "with_analytics_consent": consented,
            "levels": levels,
            "averages": {k: round(fmean(v)) if v else None for k, v in sums.items()},
            "note": "Indicador de comportamiento (180 días) inspirado en Vivek et al. (2014); "
            "no reemplaza la escala de 12 ítems ni se usa para segmentar campañas.",
        }
