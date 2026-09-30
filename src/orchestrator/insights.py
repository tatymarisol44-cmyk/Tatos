"""Insights (analytical CRM): segments, recalls, no-show risk, pipeline and a forecast.

The numbers are computed with SQL, never by the LLM; the LLM only explains them, from a
JSON of aggregates keyed by pseudonymous ids (no names, no contact data). Predefined
metrics were chosen over free text-to-SQL on purpose: with health data, a generated query
could read columns it should not, and a wrong number said confidently does more harm than
a missing chart (docs/adr/0011).

Segments are rule-based RFM (recency, frequency, monetary), with the recall interval of
the tenant's industry pack as the time unit:

    champion   >= 3 visits in 24 months, last visit within one interval
    loyal      >= 2 visits in 24 months, last visit within one interval
    new        exactly 1 visit, within one interval
    at_risk    last visit between one and two intervals ago
    dormant    last visit more than two intervals ago
    no_visits  registered, never completed a visit
    occasional anything else"""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import and_, case, func, select

from orchestrator.config import Settings
from orchestrator.crm import REVENUE_STAGES, CrmService, appointments, money, patients, treatments
from orchestrator.db import Database, aware, utcnow
from orchestrator.guardrails import check_input
from orchestrator.llm import LLMClient
from orchestrator.packs import pack_for

ANALYST_PROMPT = """You are the INSIGHTS ANALYST of a business assistant. Answer the owner's
question using ONLY the metrics in the JSON below (computed from the business database).
Name the metric you rely on in parentheses, e.g. (segments.at_risk). If the metrics do not
answer the question, say so and suggest which data would. Never invent numbers, never
guess who a patient is, and do not give clinical advice. Answer in the user's language."""

SEGMENTS = ("champion", "loyal", "new", "at_risk", "dormant", "no_visits", "occasional")


class QuestionBlockedError(ValueError):
    """The question failed the input guardrails."""

    def __init__(self, reasons: list[str]) -> None:
        super().__init__(", ".join(reasons))
        self.reasons = reasons


# Prior for the no-show rate of patients with little history (smoothing towards 10%).
_PRIOR_RATE, _PRIOR_WEIGHT = 0.1, 2.0
NO_SHOW_HIGH = 0.3


def segment_of(
    visits_24m: int, total_visits: int, recency_days: float | None, interval_days: int
) -> str:
    if total_visits == 0 or recency_days is None:
        return "no_visits"
    if recency_days > 2 * interval_days:
        return "dormant"
    if recency_days > interval_days:
        return "at_risk"
    if visits_24m >= 3:
        return "champion"
    if visits_24m >= 2:
        return "loyal"
    if total_visits == 1:
        return "new"
    return "occasional"


def no_show_rate(no_shows: int, completed: int) -> float:
    return (no_shows + _PRIOR_RATE * _PRIOR_WEIGHT) / (no_shows + completed + _PRIOR_WEIGHT)


class InsightsService:
    def __init__(self, db: Database, crm: CrmService, llm: LLMClient, settings: Settings) -> None:
        self.db = db
        self.crm = crm
        self.llm = llm
        self.settings = settings

    async def _per_patient(self, tenant: str, now: datetime) -> list[dict[str, Any]]:
        a = appointments
        completed = a.c.status == "completed"
        visits = (
            select(
                a.c.patient_id.label("pid"),
                func.sum(case((completed, 1), else_=0)).label("total_visits"),
                func.sum(
                    case((and_(completed, a.c.starts_at >= now - timedelta(days=730)), 1), else_=0)
                ).label("visits_24m"),
                func.sum(case((a.c.status == "no_show", 1), else_=0)).label("no_shows"),
                func.max(case((completed, a.c.starts_at), else_=None)).label("last_visit"),
                func.sum(case((completed, a.c.price), else_=0)).label("visit_revenue"),
            )
            .where(a.c.tenant == tenant)
            .group_by(a.c.patient_id)
            .subquery()
        )
        t = treatments
        plans = (
            select(t.c.patient_id.label("pid"), func.sum(t.c.amount).label("plan_revenue"))
            .where(and_(t.c.tenant == tenant, t.c.stage.in_(REVENUE_STAGES)))
            .group_by(t.c.patient_id)
            .subquery()
        )
        query = (
            select(
                patients.c.id,
                visits.c.total_visits,
                visits.c.visits_24m,
                visits.c.no_shows,
                visits.c.last_visit,
                visits.c.visit_revenue,
                plans.c.plan_revenue,
            )
            .select_from(
                patients.outerjoin(visits, visits.c.pid == patients.c.id).outerjoin(
                    plans, plans.c.pid == patients.c.id
                )
            )
            .where(and_(patients.c.tenant == tenant, patients.c.restricted.is_(False)))
        )
        async with self.db.engine.connect() as conn:
            rows = (await conn.execute(query)).mappings().all()
        out = []
        for r in rows:
            last = aware(r["last_visit"]) if isinstance(r["last_visit"], datetime) else None
            if last is None and r["last_visit"] is not None:  # SQLite returns text for max()
                last = aware(datetime.fromisoformat(str(r["last_visit"])))
            out.append(
                {
                    "id": r["id"],
                    "total_visits": int(r["total_visits"] or 0),
                    "visits_24m": int(r["visits_24m"] or 0),
                    "no_shows": int(r["no_shows"] or 0),
                    "last_visit": last,
                    "monetary": money(r["visit_revenue"]) + money(r["plan_revenue"]),
                }
            )
        return out

    async def segments(self, tenant: str, now: datetime | None = None) -> dict[str, list[str]]:
        now = now or utcnow()
        interval = 30 * pack_for(self.settings, tenant).crm.recall_months
        members: dict[str, list[str]] = {s: [] for s in SEGMENTS}
        for p in await self._per_patient(tenant, now):
            recency = (now - p["last_visit"]).days if p["last_visit"] else None
            members[segment_of(p["visits_24m"], p["total_visits"], recency, interval)].append(
                p["id"]
            )
        return {k: sorted(v) for k, v in members.items()}

    async def segment_members(self, tenant: str, segment: str) -> list[str]:
        if segment == "recall_due":
            return sorted(
                {a.patient_id for a in await self.crm.alerts(tenant) if a.kind == "recall_due"}
            )
        if segment == "pending_treatment":
            return sorted(
                {a.patient_id for a in await self.crm.alerts(tenant) if a.kind == "quote_followup"}
            )
        members = await self.segments(tenant)
        if segment not in members:
            raise KeyError(f"unknown segment {segment!r}")
        return members[segment]

    async def summary(self, tenant: str, now: datetime | None = None) -> dict[str, Any]:
        now = now or utcnow()
        policy = pack_for(self.settings, tenant).crm
        interval = 30 * policy.recall_months
        per_patient = await self._per_patient(tenant, now)
        by_id = {p["id"]: p for p in per_patient}
        segs: Counter[str] = Counter()
        for p in per_patient:
            recency = (now - p["last_visit"]).days if p["last_visit"] else None
            segs[segment_of(p["visits_24m"], p["total_visits"], recency, interval)] += 1

        # Top 20% by monetary value (at least one patient when anyone spent anything).
        spenders = sorted(
            (p for p in per_patient if p["monetary"] > 0), key=lambda p: -p["monetary"]
        )
        high_value = spenders[: max(1, len(spenders) // 5)] if spenders else []

        alerts = await self.crm.alerts(tenant, now)
        upcoming = await self.crm.list_appointments(tenant, start=now, end=now + timedelta(days=14))
        risky = []
        for appt in upcoming:
            if appt["status"] not in ("scheduled", "confirmed") or appt["patient_id"] not in by_id:
                continue
            p = by_id[appt["patient_id"]]
            rate = no_show_rate(p["no_shows"], p["total_visits"])
            if rate >= NO_SHOW_HIGH:
                risky.append(
                    {
                        "appointment_id": appt["id"],
                        "patient_id": appt["patient_id"],
                        "starts_at": appt["starts_at"],
                        "no_show_rate": round(rate, 2),
                        "confirmed": appt["status"] == "confirmed",
                    }
                )
        total_noshow = sum(p["no_shows"] for p in per_patient)
        total_done = sum(p["total_visits"] for p in per_patient)

        pipeline = {stage: {"count": 0, "amount": 0.0} for stage in policy.pipeline}
        for t in await self.crm.list_treatments(tenant):
            if t["patient_id"] in by_id and t["stage"] in pipeline:
                pipeline[t["stage"]]["count"] += 1
                pipeline[t["stage"]]["amount"] = round(
                    pipeline[t["stage"]]["amount"] + t["amount"], 2
                )

        # Naive forecast: the average of the last 8 weeks, projected 4 weeks ahead.
        recent = await self.crm.list_appointments(tenant, start=now - timedelta(weeks=8), end=now)
        done = [a for a in recent if a["status"] == "completed"]
        weekly = len(done) / 8
        weekly_revenue = sum(a["price"] for a in done) / 8
        return {
            "generated_at": now.isoformat(),
            "pack": pack_for(self.settings, tenant).id,
            "patients": {
                "total": len(per_patient),
                "active": sum(
                    1
                    for p in per_patient
                    if p["last_visit"] and (now - p["last_visit"]).days <= interval
                ),
            },
            "segments": {s: segs.get(s, 0) for s in SEGMENTS},
            "high_value": {
                "patients": [p["id"] for p in high_value],
                "min_monetary": high_value[-1]["monetary"] if high_value else 0.0,
            },
            "alerts": {
                level: sum(1 for a in alerts if a.level == level) for level in ("red", "yellow")
            },
            "recall_due": sum(1 for a in alerts if a.kind == "recall_due"),
            "no_show": {
                "historical_rate": round(total_noshow / (total_noshow + total_done), 3)
                if total_noshow + total_done
                else 0.0,
                "upcoming_high_risk": risky,
            },
            "pipeline": pipeline,
            "forecast": {
                "method": "8-week average",
                "completed_visits_per_week": round(weekly, 2),
                "expected_visits_next_4_weeks": round(weekly * 4, 1),
                "expected_visit_revenue_next_4_weeks": round(weekly_revenue * 4, 2),
                "scheduled_next_14_days": sum(
                    1 for a in upcoming if a["status"] in ("scheduled", "confirmed")
                ),
            },
        }

    async def ask(self, tenant: str, question: str) -> dict[str, Any]:
        guard = check_input(
            question,
            max_chars=self.settings.max_input_chars,
            injection_action=self.settings.injection_action,
            redact=self.settings.redact_pii,
        )
        if not guard.allowed:
            raise QuestionBlockedError(guard.reasons)
        question = guard.text
        metrics = await self.summary(tenant)
        data = json.dumps(metrics, ensure_ascii=False)
        result = await self.llm.complete(
            [
                {"role": "system", "content": ANALYST_PROMPT},
                {
                    "role": "user",
                    "content": f"<metrics>\n{data}\n</metrics>\n\nQuestion: {question}",
                },
            ],
            model=self.settings.llm_model,
            temperature=0.0,
        )
        return {"answer": result.text, "metrics": metrics, "usage": result.usage()}
