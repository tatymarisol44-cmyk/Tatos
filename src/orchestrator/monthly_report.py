"""The monthly report and its SMART goals (SRS RF-MKT-06).

A data-driven cycle in five steps (HubSpot; Talkwalker): evaluate, analyse, set SMART
goals, monitor, report. The practice sees, for one calendar month in its own time zone,
what happened and how it compares with the month before; the owner sets goals that are
Specific (one metric), Measurable (a number), Achievable and Relevant (her call, with a
note on why), and Time-bound (that month); progress is computed, never typed in.

Only counts of the practice leave this module, never a patient: it is the "insights"
sink of the data classes (classification.py), which health data may reach as totals."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import Column, DateTime, Float, String, Table, and_, delete, func, insert, select

from orchestrator.campaigns import campaigns
from orchestrator.crm import appointments, patients
from orchestrator.db import Database, metadata, utcnow
from orchestrator.governance import AuditLog
from orchestrator.inbox import inbox_conversations, inbox_messages
from orchestrator.instruments import instrument_assignments
from orchestrator.workshops import workshop_registrations, workshops

METRICS = {
    "nuevos_pacientes": "Pacientes nuevos",
    "citas_atendidas": "Citas atendidas",
    "inasistencias": "Inasistencias",
    "tasa_asistencia": "Asistencia (%)",
    "tests_respondidos": "Tests respondidos en la app",
    "mensajes_recibidos": "Mensajes recibidos",
    "talleres": "Talleres realizados",
    "asistentes_talleres": "Asistentes a talleres",
    "campanas_enviadas": "Campañas enviadas",
}
# For these, less is better: a goal is met at or below the target.
LOWER_IS_BETTER = {"inasistencias"}

marketing_goals = Table(
    "marketing_goals",
    metadata,
    Column("tenant", String(64), primary_key=True),
    Column("goal_id", String(32), primary_key=True),
    Column("month", String(7), nullable=False, index=True),  # YYYY-MM
    Column("metric", String(32), nullable=False),
    Column("target", Float, nullable=False),
    Column("note", String(300), nullable=False),  # why it matters: relevant, achievable
    Column("created_by", String(128), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)


class ReportError(ValueError):
    """A month or a goal the report refuses."""


def month_bounds(month: str, tz: ZoneInfo) -> tuple[datetime, datetime]:
    """The month's start and end (exclusive) in the practice's zone, as UTC instants."""
    try:
        year, mon = (int(x) for x in month.split("-"))
        start = datetime(year, mon, 1, tzinfo=tz)
    except ValueError as exc:
        raise ReportError("a month is written YYYY-MM") from exc
    end = datetime(year + (mon == 12), mon % 12 + 1, 1, tzinfo=tz)
    return start.astimezone(UTC), end.astimezone(UTC)


def previous(month: str) -> str:
    year, mon = (int(x) for x in month.split("-"))
    return f"{year - (mon == 1)}-{(mon - 2) % 12 + 1:02d}"


def highlights(now: dict[str, Any], before: dict[str, Any]) -> list[str]:
    """What stands out, in plain words, for the "analyse" step. Rules, not a model."""
    out = []
    rate, last_rate = now["tasa_asistencia"], before["tasa_asistencia"]
    if rate is not None and last_rate is not None and rate <= last_rate - 10:
        out.append(
            f"La asistencia bajó de {last_rate} % a {rate} %: revisa los recordatorios y a "
            "las pacientes con riesgo de faltar (Hoy → Pendientes)."
        )
    elif rate is not None and last_rate is not None and rate >= last_rate + 10:
        out.append(f"La asistencia subió de {last_rate} % a {rate} %.")
    if now["nuevos_pacientes"] > before["nuevos_pacientes"]:
        out.append(
            f"Llegaron {now['nuevos_pacientes']} pacientes nuevos "
            f"({before['nuevos_pacientes']} el mes anterior)."
        )
    networks = now["mensajes_por_red"]
    if networks:
        top = max(networks, key=lambda n: networks[n])
        out.append(f"La red con más mensajes fue {top.capitalize()} ({networks[top]}).")
    if not now["talleres"]:
        out.append(
            "No hubo talleres este mes: son la forma de conexión social con la comunidad "
            "(compromiso y fidelización)."
        )
    return out


class MonthlyReport:
    def __init__(self, db: Database, audit: AuditLog, tz: ZoneInfo) -> None:
        self.db = db
        self.audit = audit
        self.tz = tz

    def this_month(self) -> str:
        now = utcnow().astimezone(self.tz)
        return f"{now.year}-{now.month:02d}"

    async def metrics(self, tenant: str, month: str) -> dict[str, Any]:
        start, end = month_bounds(month, self.tz)

        def inside(column: Any) -> Any:
            return and_(column >= start, column < end)

        async with self.db.engine.connect() as conn:

            async def count(query: Any) -> int:
                return int((await conn.execute(query)).scalar_one() or 0)

            new = await count(
                select(func.count()).where(
                    and_(patients.c.tenant == tenant, inside(patients.c.created_at))
                )
            )
            completed, missed = (
                await conn.execute(
                    select(
                        func.count().filter(appointments.c.status == "completed"),
                        func.count().filter(appointments.c.status == "no_show"),
                    ).where(and_(appointments.c.tenant == tenant, inside(appointments.c.starts_at)))
                )
            ).one()
            answered = await count(
                select(func.count()).where(
                    and_(
                        instrument_assignments.c.tenant == tenant,
                        instrument_assignments.c.status == "completed",
                        inside(instrument_assignments.c.completed_at),
                    )
                )
            )
            by_network = {
                r.network: int(r.n)
                for r in await conn.execute(
                    select(inbox_conversations.c.network, func.count().label("n"))
                    .select_from(
                        inbox_messages.join(
                            inbox_conversations,
                            and_(
                                inbox_conversations.c.tenant == inbox_messages.c.tenant,
                                inbox_conversations.c.conversation_id
                                == inbox_messages.c.conversation_id,
                            ),
                        )
                    )
                    .where(
                        and_(
                            inbox_messages.c.tenant == tenant,
                            inbox_messages.c.direction == "in",
                            inside(inbox_messages.c.created_at),
                        )
                    )
                    .group_by(inbox_conversations.c.network)
                )
            }
            held = await count(
                select(func.count()).where(
                    and_(
                        workshops.c.tenant == tenant,
                        workshops.c.status != "cancelled",
                        inside(workshops.c.starts_at),
                    )
                )
            )
            attendees = await count(
                select(func.count())
                .select_from(
                    workshop_registrations.join(
                        workshops,
                        and_(
                            workshops.c.tenant == workshop_registrations.c.tenant,
                            workshops.c.workshop_id == workshop_registrations.c.workshop_id,
                        ),
                    )
                )
                .where(
                    and_(
                        workshop_registrations.c.tenant == tenant,
                        workshop_registrations.c.status == "attended",
                        inside(workshops.c.starts_at),
                    )
                )
            )
            sent = await count(
                select(func.count()).where(
                    and_(campaigns.c.tenant == tenant, inside(campaigns.c.sent_at))
                )
            )
        decided = completed + missed
        return {
            "nuevos_pacientes": new,
            "citas_atendidas": int(completed),
            "inasistencias": int(missed),
            "tasa_asistencia": round(100 * completed / decided) if decided else None,
            "tests_respondidos": answered,
            "mensajes_recibidos": sum(by_network.values()),
            "mensajes_por_red": by_network,
            "talleres": held,
            "asistentes_talleres": attendees,
            "campanas_enviadas": sent,
        }

    async def report(self, tenant: str, month: str, actor: str) -> dict[str, Any]:
        now, before = await self.metrics(tenant, month), await self.metrics(tenant, previous(month))
        goals = await self.goals(tenant, month)
        for goal in goals:
            value = now.get(goal["metric"])
            goal["value"] = value
            if value is None:
                goal["progress"], goal["met"] = None, False
            elif goal["metric"] in LOWER_IS_BETTER:
                goal["met"] = value <= goal["target"]
                goal["progress"] = 100 if goal["met"] else round(100 * goal["target"] / value)
            else:
                goal["progress"] = min(100, round(100 * value / goal["target"]))
                goal["met"] = value >= goal["target"]
        await self.audit.record(tenant, actor, "report.viewed", f"report/{month}")
        return {
            "month": month,
            "previous": previous(month),
            "labels": METRICS,
            "metrics": now,
            "before": before,
            "highlights": highlights(now, before),
            "goals": goals,
        }

    async def goals(self, tenant: str, month: str) -> list[dict[str, Any]]:
        query = (
            select(marketing_goals)
            .where(and_(marketing_goals.c.tenant == tenant, marketing_goals.c.month == month))
            .order_by(marketing_goals.c.created_at)
        )
        async with self.db.engine.connect() as conn:
            rows = (await conn.execute(query)).mappings().all()
        return [
            {
                "goal_id": r["goal_id"],
                "month": r["month"],
                "metric": r["metric"],
                "label": METRICS[r["metric"]],
                "target": r["target"],
                "note": r["note"],
            }
            for r in rows
        ]

    async def add_goal(
        self, tenant: str, actor: str, *, month: str, metric: str, target: float, note: str
    ) -> dict[str, Any]:
        if metric not in METRICS:
            raise ReportError(f"unknown metric {metric!r}")
        month_bounds(month, self.tz)  # a valid month
        if month < self.this_month():
            raise ReportError("goals are set for this month or a later one")
        if metric == "tasa_asistencia" and not 0 < target <= 100:
            raise ReportError("attendance is a percentage")
        goal_id = uuid.uuid4().hex[:16]
        async with self.db.engine.begin() as conn:
            await conn.execute(
                insert(marketing_goals).values(
                    tenant=tenant,
                    goal_id=goal_id,
                    month=month,
                    metric=metric,
                    target=target,
                    note=note,
                    created_by=actor,
                    created_at=utcnow(),
                )
            )
            await self.audit.record_in(conn, tenant, actor, "report.goal_set", f"goal/{goal_id}")
        [made] = [g for g in await self.goals(tenant, month) if g["goal_id"] == goal_id]
        return made

    async def remove_goal(self, tenant: str, goal_id: str, actor: str) -> bool:
        async with self.db.engine.begin() as conn:
            done = await conn.execute(
                delete(marketing_goals).where(
                    and_(marketing_goals.c.tenant == tenant, marketing_goals.c.goal_id == goal_id)
                )
            )
            if done.rowcount:
                await self.audit.record_in(
                    conn, tenant, actor, "report.goal_removed", f"goal/{goal_id}"
                )
        return bool(done.rowcount)
