"""Patient follow-up for the treating professional: how the patient attends, how their
tests evolve, what needs attention. Computed on request from the record; nothing new is
stored and no model is involved, so every flag can be explained.

- **Attendance**: completed, missed (no-show) and cancelled visits, attendance rate, days
  since the last visit, whether a next visit is booked.
- **No-show risk**: a transparent rule, not a black box: earlier no-shows, a recent
  no-show, late cancellations, no booked follow-up. It drives reminders, never a judgement
  about the person, and its reasons are always shown.
- **Test trends**: for each instrument applied twice or more, the first and last score
  and whether the severity band went up or down. A band is an aid, not a diagnosis.
- **Engagement with campaigns**: only for patients who granted the analytics consent
  (profiling, GDPR Art. 22 / LOPDP); otherwise it is not computed at all.
Legal basis for the rest: the care itself (GDPR Art. 9(2)(h)); clinicians only.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import and_, select

from orchestrator.campaigns import recipients
from orchestrator.crm import appointments, patients
from orchestrator.db import Database, aware
from orchestrator.governance import ConsentRegistry, Purpose
from orchestrator.instruments import instrument_results

SEVERITY_ORDER = {None: 0, "none": 0, "low": 1, "moderate": 2, "high": 3}
OVERDUE_DAYS = 30


def no_show_risk(
    completed: int, no_shows: int, cancelled: int, recent_no_show: bool, has_next: bool
) -> dict[str, Any]:
    """'high', 'medium' or 'low', with the reasons, from the visit history."""
    reasons = []
    total = completed + no_shows
    rate = no_shows / total if total else 0.0
    many = (total >= 2 and rate >= 0.5) or (total >= 3 and rate >= 0.3)
    if many:
        reasons.append(f"missed {no_shows} of {total} visits")
    if recent_no_show:
        reasons.append("missed the last visit")
    if cancelled >= 2 and cancelled >= completed:
        reasons.append(f"cancelled {cancelled} visits")
    if total and not has_next:
        reasons.append("no follow-up visit booked")
    # Every level above "low" carries its reasons: a flag nobody can explain is not shown.
    high = len(reasons) >= 2 or (total >= 2 and rate >= 0.5)
    level = "high" if high else "medium" if reasons else "low"
    return {"level": level, "reasons": reasons}


def trend(results: list[dict[str, Any]]) -> dict[str, Any]:
    first, last = results[0], results[-1]
    before, after = (
        SEVERITY_ORDER.get(first["severity"], 0),
        SEVERITY_ORDER.get(last["severity"], 0),
    )
    direction = "worse" if after > before else "better" if after < before else "same"
    return {
        "instrument": last["instrument_name"],
        "applied": len(results),
        "first": {"total": first["total"], "band": first["band"], "at": first["created_at"]},
        "last": {"total": last["total"], "band": last["band"], "at": last["created_at"]},
        "direction": direction,
        "alerts_last": [a["message"] for a in last["alerts"]],
    }


class FollowUp:
    def __init__(self, db: Database, consents: ConsentRegistry) -> None:
        self.db = db
        self.consents = consents

    async def patient(
        self, tenant: str, patient_id: str, now: datetime | None = None
    ) -> dict[str, Any]:
        now = now or datetime.now(UTC)
        async with self.db.engine.connect() as conn:
            known = (
                await conn.execute(
                    select(patients.c.display_name).where(
                        and_(patients.c.tenant == tenant, patients.c.id == patient_id)
                    )
                )
            ).first()
            if known is None:
                raise KeyError(patient_id)
            visits = list(
                await conn.execute(
                    select(appointments.c.starts_at, appointments.c.status)
                    .where(
                        and_(
                            appointments.c.tenant == tenant, appointments.c.patient_id == patient_id
                        )
                    )
                    .order_by(appointments.c.starts_at)
                )
            )
            tests = list(
                await conn.execute(
                    select(instrument_results)
                    .where(
                        and_(
                            instrument_results.c.tenant == tenant,
                            instrument_results.c.patient_id == patient_id,
                        )
                    )
                    .order_by(instrument_results.c.created_at)
                )
            )
        past = [(aware(s), st) for s, st in visits if (aware(s) or now) <= now]
        future = [
            (aware(s), st)
            for s, st in visits
            if (aware(s) or now) > now and st in ("scheduled", "confirmed")
        ]
        completed = sum(1 for _, st in past if st == "completed")
        no_shows = sum(1 for _, st in past if st == "no_show")
        cancelled = sum(1 for _, st in visits if st == "cancelled")
        attended = [s for s, st in past if st == "completed" and s is not None]
        last_visit = max(attended) if attended else None
        decided = [st for _, st in past if st in ("completed", "no_show")]
        attendance = {
            "completed": completed,
            "no_shows": no_shows,
            "cancelled": cancelled,
            "attendance_rate": round(completed / (completed + no_shows), 2)
            if completed + no_shows
            else None,
            "last_visit": last_visit.isoformat() if last_visit else None,
            "days_since_last_visit": (now - last_visit).days if last_visit else None,
            "next_visit": future[0][0].isoformat() if future and future[0][0] else None,
        }
        risk = no_show_risk(
            completed, no_shows, cancelled, bool(decided) and decided[-1] == "no_show", bool(future)
        )

        by_instrument: dict[str, list[dict[str, Any]]] = {}
        for r in tests:
            by_instrument.setdefault(r.instrument_id, []).append(
                {
                    "instrument_name": r.instrument_name,
                    "total": r.total,
                    "band": r.band,
                    "severity": r.severity,
                    "alerts": r.alerts,
                    "created_at": (aware(r.created_at) or now).isoformat(),
                }
            )
        trends = [trend(rows) for rows in by_instrument.values()]

        engagement: dict[str, Any] = {"available": False, "reason": "no analytics consent"}
        if await self.consents.has(tenant, patient_id, Purpose.ANALYTICS):
            async with self.db.engine.connect() as conn:
                messages = list(
                    await conn.execute(
                        select(recipients.c.status, recipients.c.seen_at).where(
                            and_(
                                recipients.c.tenant == tenant,
                                recipients.c.patient_id == patient_id,
                                recipients.c.arm == "treatment",
                            )
                        )
                    )
                )
            engagement = {
                "available": True,
                "campaign_messages": len(messages),
                "seen": sum(1 for _, seen in messages if seen is not None),
            }

        flags = []
        if risk["level"] == "high":
            flags.append("no-show risk")
        if any(t["direction"] == "worse" for t in trends):
            flags.append("a test got worse")
        if any(t["alerts_last"] for t in trends):
            flags.append("test alert")
        if last_visit is not None and (now - last_visit).days > OVERDUE_DAYS and not future:
            flags.append(f"no visit for over {OVERDUE_DAYS} days and none booked")
        return {
            "patient_id": patient_id,
            "name": known.display_name,
            "attendance": attendance,
            "no_show_risk": risk,
            "tests": trends,
            "engagement": engagement,
            "flags": flags,
        }

    async def needing_attention(self, tenant: str, limit: int = 50) -> list[dict[str, Any]]:
        """Patients with at least one flag, most flags first: the professional's worklist."""
        async with self.db.engine.connect() as conn:
            ids = [
                r.id
                for r in await conn.execute(
                    select(patients.c.id).where(
                        and_(patients.c.tenant == tenant, patients.c.restricted.is_(False))
                    )
                )
            ]
        out = []
        for pid in ids:
            view = await self.patient(tenant, pid)
            if view["flags"]:
                out.append({"patient_id": pid, "name": view["name"], "flags": view["flags"]})
        out.sort(key=lambda v: (-len(v["flags"]), v["name"]))
        return out[:limit]
