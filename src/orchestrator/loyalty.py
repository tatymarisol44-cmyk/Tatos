"""Loyalty tiers for a mental-health practice (SRS RF-MKT-02), never monetary.

Tiers, points and non-monetary rewards raise retention, and making the benefits known is
what makes them work (Bariha, 2020). A practice of psychology must not reward *more
sessions*, so nothing here counts sessions or money:

* the tier comes from time in care (months since the first completed visit) and from
  taking part in the practice's community (workshops attended, RF-MKT-05);
* the benefits are access, not discounts: a longer booking window in the patient's app
  and an earlier sign-up for workshops. Each benefit listed is enforced by the code
  (`/v1/me/slots`, `Workshops.register`), so the app never promises what it does not do.

The tier is the patient's own information (shown in their app) and the practice's (shown
to its staff). Using it to choose whom a campaign reaches is profiling, which needs the
analytics consent (campaigns.py), like every other segment."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import and_, func, select

from orchestrator.crm import appointments
from orchestrator.db import Database, aware, utcnow
from orchestrator.workshops import workshop_registrations


@dataclass(frozen=True)
class Tier:
    key: str
    name: str
    booking_days: int  # how far ahead the patient can book in the app
    early_hours: int  # how long before everyone else they can sign up for a workshop
    benefits: tuple[str, ...]


TIERS = (
    Tier(
        "bienvenida",
        "Bienvenida",
        21,
        0,
        ("Reserva tus citas en la app hasta 3 semanas antes.",),
    ),
    Tier(
        "constancia",
        "Constancia",
        30,
        48,
        (
            "Reserva tus citas en la app hasta 30 días antes.",
            "Inscríbete en los talleres 48 horas antes que el resto.",
        ),
    ),
    Tier(
        "comunidad",
        "Comunidad",
        45,
        96,
        (
            "Reserva tus citas en la app hasta 45 días antes.",
            "Inscríbete en los talleres 4 días antes que el resto.",
        ),
    ),
)
BY_KEY = {t.key: t for t in TIERS}


def tier_for(months_in_care: int, workshops_attended: int) -> Tier:
    """The rule, shown to the patient as it is: time in care or community, never sessions."""
    if months_in_care >= 6 or workshops_attended >= 2:
        return BY_KEY["comunidad"]
    if months_in_care >= 3 or workshops_attended >= 1:
        return BY_KEY["constancia"]
    return BY_KEY["bienvenida"]


def months_between(start: datetime, end: datetime) -> int:
    months = (end.year - start.year) * 12 + end.month - start.month
    return months - (1 if end.day < start.day else 0)


class Loyalty:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def for_patient(self, tenant: str, patient_id: str) -> dict[str, Any]:
        async with self.db.engine.connect() as conn:
            first = (
                await conn.execute(
                    select(func.min(appointments.c.starts_at)).where(
                        and_(
                            appointments.c.tenant == tenant,
                            appointments.c.patient_id == patient_id,
                            appointments.c.status == "completed",
                        )
                    )
                )
            ).scalar()
            attended = (
                await conn.execute(
                    select(func.count()).where(
                        and_(
                            workshop_registrations.c.tenant == tenant,
                            workshop_registrations.c.patient_id == patient_id,
                            workshop_registrations.c.status == "attended",
                        )
                    )
                )
            ).scalar_one()
        since = aware(first)
        months = max(0, months_between(since, utcnow())) if since else 0
        tier = tier_for(months, int(attended))
        following = next((t for t in TIERS if TIERS.index(t) == TIERS.index(tier) + 1), None)
        return {
            "tier": tier.key,
            "name": tier.name,
            "benefits": list(tier.benefits),
            "booking_days": tier.booking_days,
            "early_hours": tier.early_hours,
            "months_in_care": months,
            "workshops_attended": int(attended),
            "next": {
                "name": following.name,
                "how": "Con 6 meses de acompañamiento o 2 talleres."
                if following.key == "comunidad"
                else "Con 3 meses de acompañamiento o 1 taller.",
            }
            if following
            else None,
        }
