"""CRM: customers/patients, appointments and treatment plans (quotes), plus the
traffic-light alerts that keep follow-ups from slipping.

Vocabulary is health-flavoured because the first vertical is dental, but nothing here is
dental-specific: pipeline stages, recall interval and alert thresholds come from the
tenant's industry pack.

A patient's `id` is the pseudonymous subject id used everywhere else (consents, memory,
audit, campaigns). Contact data (phone, e-mail, Telegram) is only used for the
appointment itself and, with the `marketing` consent, for campaigns.

Privacy:
- every write, and every time a person opens a patient record, is an audit event
  (HIPAA 164.312(b)); listings return no contact data;
- erasure removes contact data and marks the record `restricted`: the clinical record
  itself (appointments, treatments) is kept for the legal retention period
  (GDPR Art. 17(3)(b) legal obligation, 17(3)(c) public health); restricted records are
  excluded from insights and campaigns."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    Date,
    DateTime,
    Integer,
    Numeric,
    String,
    Table,
    and_,
    func,
    insert,
    select,
    update,
)

from orchestrator.config import Settings
from orchestrator.db import Database, metadata, utcnow
from orchestrator.governance import AuditLog
from orchestrator.packs import pack_for

patients = Table(
    "crm_patients",
    metadata,
    Column("tenant", String(64), primary_key=True),
    Column("id", String(64), primary_key=True),
    Column("display_name", String(200), nullable=False),
    Column("phone", String(32), nullable=True),
    Column("email", String(254), nullable=True),
    Column("telegram_chat_id", String(64), nullable=True),
    Column("birth_date", Date, nullable=True),
    Column("preferred_channel", String(16), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("restricted", Boolean, nullable=False, default=False),
    Column("erased_at", DateTime(timezone=True), nullable=True),
)

appointments = Table(
    "crm_appointments",
    metadata,
    Column("tenant", String(64), primary_key=True),
    Column("id", String(64), primary_key=True),
    Column("patient_id", String(64), nullable=False, index=True),
    Column("starts_at", DateTime(timezone=True), nullable=False, index=True),
    Column("duration_min", Integer, nullable=False),
    Column("kind", String(32), nullable=False),
    Column("status", String(16), nullable=False),
    Column("price", Numeric(12, 2), nullable=False, default=0),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    # Who sees the patient: the professional's agenda never holds two overlapping visits.
    Column("professional_id", String(64), nullable=True, index=True),
    # When the day-before reminder was claimed (reminders.py): one per visit.
    Column("reminded_at", DateTime(timezone=True), nullable=True),
)
ACTIVE_STATUSES = ("scheduled", "confirmed")

treatments = Table(
    "crm_treatments",
    metadata,
    Column("tenant", String(64), primary_key=True),
    Column("id", String(64), primary_key=True),
    Column("patient_id", String(64), nullable=False, index=True),
    Column("title", String(200), nullable=False),
    Column("amount", Numeric(12, 2), nullable=False),
    Column("stage", String(16), nullable=False),
    Column("presented_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

# Conversations with clinical content, kept as part of the clinical record when the
# conversation itself is deleted (erasure request, retention purge). Only what the
# patient was actually shown, plus who reviewed it: never an unreviewed AI draft.
clinical_notes = Table(
    "crm_clinical_notes",
    metadata,
    Column("tenant", String(64), primary_key=True),
    Column("thread_id", String(128), primary_key=True),
    Column("patient_id", String(64), nullable=False, index=True),
    Column("messages", JSON, nullable=False),
    Column("review", JSON, nullable=True),
    Column("reason", String(32), nullable=False),  # erasure_request | retention
    Column("archived_at", DateTime(timezone=True), nullable=False),
)

APPOINTMENT_STATUSES = ("scheduled", "confirmed", "completed", "no_show", "cancelled")
_TRANSITIONS = {
    "scheduled": {"confirmed", "completed", "no_show", "cancelled"},
    "confirmed": {"completed", "no_show", "cancelled"},
    "completed": set(),
    "no_show": set(),
    "cancelled": set(),
}
# Treatment stages that count as revenue (accepted work).
REVENUE_STAGES = ("accepted", "in_progress", "completed")


class CrmError(ValueError):
    """Invalid CRM operation (unknown record, illegal transition, restricted patient)."""


class SlotTaken(CrmError):
    """The professional already has a visit that overlaps this time."""


class NotFoundError(KeyError):
    pass


def money(value: Any) -> float:
    return float(Decimal(str(value or 0)).quantize(Decimal("0.01")))


def _utc(value: datetime) -> datetime:
    """The same instant in UTC (A24). Every timestamp is stored in UTC, so a stored value
    means the same on SQLite (which drops offsets) and Postgres. The API only accepts
    times with an offset; a naive value here comes from our own code and is UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


@dataclass(frozen=True)
class Alert:
    kind: str  # appointment_unconfirmed | quote_followup | recall_due
    level: str  # yellow | red
    patient_id: str
    ref_id: str | None
    message: str
    due_at: str

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def _row(row: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in dict(row).items():
        if isinstance(value, datetime):
            out[key] = _utc(value).isoformat()
        elif isinstance(value, date):
            out[key] = value.isoformat()
        elif isinstance(value, Decimal):
            out[key] = money(value)
        else:
            out[key] = value
    return out


class CrmService:
    def __init__(self, db: Database, audit: AuditLog, settings: Settings) -> None:
        self.db = db
        self.audit = audit
        self.settings = settings

    # --- patients ------------------------------------------------------------
    async def create_patient(
        self, tenant: str, data: dict[str, Any], actor: str, patient_id: str | None = None
    ) -> dict[str, Any]:
        pid = patient_id or uuid.uuid4().hex[:12]
        if await self._patient(tenant, pid) is not None:
            raise CrmError(f"patient {pid} already exists")
        values = {
            "display_name": data["display_name"],
            "phone": data.get("phone"),
            "email": data.get("email"),
            "telegram_chat_id": data.get("telegram_chat_id"),
            "birth_date": data.get("birth_date"),
            "preferred_channel": data.get("preferred_channel"),
        }
        async with self.db.engine.begin() as conn:
            await conn.execute(
                insert(patients).values(
                    tenant=tenant, id=pid, created_at=utcnow(), restricted=False, **values
                )
            )
            await self.audit.record_in(
                conn, tenant, actor, "crm.patient.created", "patient", subject_id=pid
            )
        return await self.get_patient(tenant, pid, actor=None)

    async def _patient(self, tenant: str, patient_id: str) -> dict[str, Any] | None:
        query = select(patients).where(
            and_(patients.c.tenant == tenant, patients.c.id == patient_id)
        )
        async with self.db.engine.connect() as conn:
            row = (await conn.execute(query)).mappings().first()
        return _row(row) if row else None

    async def get_patient(self, tenant: str, patient_id: str, actor: str | None) -> dict[str, Any]:
        patient = await self._patient(tenant, patient_id)
        if patient is None:
            raise NotFoundError(patient_id)
        if actor is not None:  # every human read of a record is logged
            await self.audit.record(
                tenant, actor, "crm.patient.viewed", "patient", subject_id=patient_id
            )
        return patient

    async def list_patients(
        self, tenant: str, *, search: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        query = select(
            patients.c.id,
            patients.c.display_name,
            patients.c.preferred_channel,
            patients.c.restricted,
            patients.c.created_at,
        ).where(patients.c.tenant == tenant)
        if search:
            query = query.where(patients.c.display_name.ilike(f"%{search}%"))
        query = query.order_by(patients.c.display_name).limit(limit)
        async with self.db.engine.connect() as conn:
            rows = (await conn.execute(query)).mappings().all()
        return [_row(r) for r in rows]

    async def update_patient(
        self, tenant: str, patient_id: str, changes: dict[str, Any], actor: str
    ) -> dict[str, Any]:
        patient = await self.get_patient(tenant, patient_id, actor=None)
        if patient["restricted"]:
            raise CrmError("patient record is restricted after an erasure request")
        allowed = {
            "display_name",
            "phone",
            "email",
            "telegram_chat_id",
            "birth_date",
            "preferred_channel",
        }
        values = {k: v for k, v in changes.items() if k in allowed}
        if values:
            query = (
                update(patients)
                .where(and_(patients.c.tenant == tenant, patients.c.id == patient_id))
                .values(**values)
            )
            async with self.db.engine.begin() as conn:
                await conn.execute(query)
                await self.audit.record_in(
                    conn,
                    tenant,
                    actor,
                    "crm.patient.updated",
                    "patient",
                    subject_id=patient_id,
                    details={"fields": sorted(values)},
                )
        return await self.get_patient(tenant, patient_id, actor=None)

    async def contacts(self, tenant: str, patient_ids: list[str]) -> dict[str, dict[str, Any]]:
        """Name, channel addresses and restriction flag for many patients in one query
        (campaign delivery). Not audited per patient: the campaign send is."""
        if not patient_ids:
            return {}
        query = select(
            patients.c.id,
            patients.c.display_name,
            patients.c.telegram_chat_id,
            patients.c.restricted,
        ).where(and_(patients.c.tenant == tenant, patients.c.id.in_(patient_ids)))
        async with self.db.engine.connect() as conn:
            rows = (await conn.execute(query)).mappings().all()
        return {r["id"]: dict(r) for r in rows}

    async def _require_active(self, tenant: str, patient_id: str) -> dict[str, Any]:
        patient = await self._patient(tenant, patient_id)
        if patient is None:
            raise NotFoundError(patient_id)
        if patient["restricted"]:
            raise CrmError("patient record is restricted after an erasure request")
        return patient

    # --- appointments --------------------------------------------------------
    async def create_appointment(
        self,
        tenant: str,
        patient_id: str,
        *,
        starts_at: datetime,
        duration_min: int,
        kind: str,
        price: float,
        actor: str,
        professional_id: str | None = None,
    ) -> dict[str, Any]:
        await self._require_active(tenant, patient_id)
        aid = uuid.uuid4().hex[:12]
        now = utcnow()
        start = _utc(starts_at)
        async with self.db.engine.begin() as conn:
            if professional_id is not None:
                await self._claim_time(conn, tenant, professional_id, start, duration_min)
            await conn.execute(
                insert(appointments).values(
                    tenant=tenant,
                    id=aid,
                    patient_id=patient_id,
                    starts_at=_utc(starts_at),
                    duration_min=duration_min,
                    kind=kind,
                    status="scheduled",
                    price=Decimal(str(price)),
                    created_at=now,
                    updated_at=now,
                    professional_id=professional_id,
                )
            )
            await self.audit.record_in(
                conn,
                tenant,
                actor,
                "crm.appointment.created",
                f"appointment/{aid}",
                subject_id=patient_id,
            )
        return await self.get_appointment(tenant, aid)

    async def _claim_time(
        self, conn: Any, tenant: str, professional_id: str, start: datetime, minutes: int
    ) -> None:
        """Inside the booking transaction: lock the professional's row (two bookings for the
        same professional wait for each other, on any replica), then refuse an overlap."""
        from orchestrator.establishment import professionals  # avoids an import cycle

        owner = await conn.execute(
            select(professionals.c.professional_id)
            .where(
                and_(
                    professionals.c.tenant == tenant,
                    professionals.c.professional_id == professional_id,
                    professionals.c.active.is_(True),
                )
            )
            .with_for_update()
        )
        if owner.first() is None:
            raise CrmError(f"unknown or inactive professional {professional_id!r}")
        end = start + timedelta(minutes=minutes)
        nearby = await conn.execute(
            select(appointments.c.starts_at, appointments.c.duration_min).where(
                and_(
                    appointments.c.tenant == tenant,
                    appointments.c.professional_id == professional_id,
                    appointments.c.status.in_(ACTIVE_STATUSES),
                    appointments.c.starts_at < end,
                    appointments.c.starts_at > start - timedelta(hours=12),
                )
            )
        )
        for other_start, other_minutes in nearby:
            other = _utc(other_start)
            if other < end and other + timedelta(minutes=other_minutes) > start:
                raise SlotTaken("the professional already has a visit at that time")

    async def get_appointment(self, tenant: str, appointment_id: str) -> dict[str, Any]:
        query = select(appointments).where(
            and_(appointments.c.tenant == tenant, appointments.c.id == appointment_id)
        )
        async with self.db.engine.connect() as conn:
            row = (await conn.execute(query)).mappings().first()
        if row is None:
            raise NotFoundError(appointment_id)
        return _row(row)

    async def list_appointments(
        self,
        tenant: str,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        patient_id: str | None = None,
    ) -> list[dict[str, Any]]:
        query = select(appointments).where(appointments.c.tenant == tenant)
        if start is not None:
            query = query.where(appointments.c.starts_at >= _utc(start))
        if end is not None:
            query = query.where(appointments.c.starts_at < _utc(end))
        if patient_id is not None:
            query = query.where(appointments.c.patient_id == patient_id)
        async with self.db.engine.connect() as conn:
            rows = (await conn.execute(query.order_by(appointments.c.starts_at))).mappings().all()
        return [_row(r) for r in rows]

    async def set_appointment_status(
        self, tenant: str, appointment_id: str, status: str, actor: str
    ) -> dict[str, Any]:
        current = await self.get_appointment(tenant, appointment_id)
        if status not in _TRANSITIONS.get(current["status"], set()):
            raise CrmError(f"cannot move an appointment from {current['status']} to {status}")
        # Conditional on the status just checked (A23): if another request moved the
        # appointment meanwhile, this one changes nothing and gets a conflict.
        query = (
            update(appointments)
            .where(
                and_(
                    appointments.c.tenant == tenant,
                    appointments.c.id == appointment_id,
                    appointments.c.status == current["status"],
                )
            )
            .values(status=status, updated_at=utcnow())
        )
        async with self.db.engine.begin() as conn:
            if (await conn.execute(query)).rowcount != 1:
                raise CrmError("the appointment changed meanwhile; reload it")
            await self.audit.record_in(
                conn,
                tenant,
                actor,
                f"crm.appointment.{status}",
                f"appointment/{appointment_id}",
                subject_id=current["patient_id"],
            )
        return await self.get_appointment(tenant, appointment_id)

    # --- treatment plans (quotes) --------------------------------------------
    async def create_treatment(
        self, tenant: str, patient_id: str, *, title: str, amount: float, actor: str
    ) -> dict[str, Any]:
        await self._require_active(tenant, patient_id)
        tid = uuid.uuid4().hex[:12]
        now = utcnow()
        stage = pack_for(self.settings, tenant).crm.pipeline[0]
        async with self.db.engine.begin() as conn:
            await conn.execute(
                insert(treatments).values(
                    tenant=tenant,
                    id=tid,
                    patient_id=patient_id,
                    title=title,
                    amount=Decimal(str(amount)),
                    stage=stage,
                    presented_at=now,
                    updated_at=now,
                )
            )
            await self.audit.record_in(
                conn,
                tenant,
                actor,
                "crm.treatment.created",
                f"treatment/{tid}",
                subject_id=patient_id,
            )
        return await self.get_treatment(tenant, tid)

    async def get_treatment(self, tenant: str, treatment_id: str) -> dict[str, Any]:
        query = select(treatments).where(
            and_(treatments.c.tenant == tenant, treatments.c.id == treatment_id)
        )
        async with self.db.engine.connect() as conn:
            row = (await conn.execute(query)).mappings().first()
        if row is None:
            raise NotFoundError(treatment_id)
        return _row(row)

    async def list_treatments(
        self, tenant: str, *, patient_id: str | None = None, stage: str | None = None
    ) -> list[dict[str, Any]]:
        query = select(treatments).where(treatments.c.tenant == tenant)
        if patient_id is not None:
            query = query.where(treatments.c.patient_id == patient_id)
        if stage is not None:
            query = query.where(treatments.c.stage == stage)
        async with self.db.engine.connect() as conn:
            rows = (await conn.execute(query.order_by(treatments.c.presented_at))).mappings().all()
        return [_row(r) for r in rows]

    async def set_treatment_stage(
        self, tenant: str, treatment_id: str, stage: str, actor: str
    ) -> dict[str, Any]:
        pipeline = pack_for(self.settings, tenant).crm.pipeline
        if stage not in pipeline:
            raise CrmError(f"unknown stage {stage!r}; pipeline: {pipeline}")
        current = await self.get_treatment(tenant, treatment_id)
        query = (
            update(treatments)
            .where(
                and_(
                    treatments.c.tenant == tenant,
                    treatments.c.id == treatment_id,
                    treatments.c.stage == current["stage"],
                )
            )
            .values(stage=stage, updated_at=utcnow())
        )
        async with self.db.engine.begin() as conn:
            if (await conn.execute(query)).rowcount != 1:
                raise CrmError("the treatment plan changed meanwhile; reload it")
            await self.audit.record_in(
                conn,
                tenant,
                actor,
                "crm.treatment.stage",
                f"treatment/{treatment_id}",
                subject_id=current["patient_id"],
                details={"from": current["stage"], "to": stage},
            )
        return await self.get_treatment(tenant, treatment_id)

    # --- traffic-light alerts ------------------------------------------------
    async def alerts(self, tenant: str, now: datetime | None = None) -> list[Alert]:
        """Yellow/red alerts, red first. Idea from the Starwood e-CRM case: a request that
        is not handled in time changes colour until someone acts on it."""
        now = now or utcnow()
        policy = pack_for(self.settings, tenant).crm
        active = select(patients.c.id).where(
            and_(patients.c.tenant == tenant, patients.c.restricted.is_(False))
        )
        out: list[Alert] = []
        async with self.db.engine.connect() as conn:
            # 1. Appointments coming up that the patient has not confirmed.
            upcoming = (
                (
                    await conn.execute(
                        select(appointments).where(
                            and_(
                                appointments.c.tenant == tenant,
                                appointments.c.status == "scheduled",
                                appointments.c.starts_at > now,
                                appointments.c.starts_at
                                <= now + timedelta(hours=policy.confirm_yellow_hours),
                                appointments.c.patient_id.in_(active),
                            )
                        )
                    )
                )
                .mappings()
                .all()
            )
            for a in upcoming:
                starts = _utc(a["starts_at"])
                red = starts - now <= timedelta(hours=policy.confirm_red_hours)
                out.append(
                    Alert(
                        "appointment_unconfirmed",
                        "red" if red else "yellow",
                        a["patient_id"],
                        a["id"],
                        f"Appointment on {starts.isoformat()} is not confirmed",
                        starts.isoformat(),
                    )
                )
            # 2. Quotes presented and not answered.
            first_stage = pack_for(self.settings, tenant).crm.pipeline[0]
            stale = (
                (
                    await conn.execute(
                        select(treatments).where(
                            and_(
                                treatments.c.tenant == tenant,
                                treatments.c.stage == first_stage,
                                treatments.c.presented_at
                                <= now - timedelta(days=policy.quote_followup_days),
                                treatments.c.patient_id.in_(active),
                            )
                        )
                    )
                )
                .mappings()
                .all()
            )
            for t in stale:
                presented = _utc(t["presented_at"])
                red = now - presented >= timedelta(days=2 * policy.quote_followup_days)
                out.append(
                    Alert(
                        "quote_followup",
                        "red" if red else "yellow",
                        t["patient_id"],
                        t["id"],
                        f"Treatment plan '{t['title']}' has had no answer since "
                        f"{presented.date().isoformat()}",
                        (presented + timedelta(days=policy.quote_followup_days)).isoformat(),
                    )
                )
        # 3. Patients due for their recall (last completed visit older than the interval)
        #    who have not booked their next visit yet.
        booked = await self._with_future_appointment(tenant, now)
        for patient_id, last in (await self.last_visits(tenant)).items():
            due = last + timedelta(days=30 * policy.recall_months)
            if due > now or patient_id in booked:
                continue
            red = now >= due + timedelta(days=60)
            out.append(
                Alert(
                    "recall_due",
                    "red" if red else "yellow",
                    patient_id,
                    None,
                    f"Recall due since {due.date().isoformat()} (last visit "
                    f"{last.date().isoformat()})",
                    due.isoformat(),
                )
            )
        return sorted(out, key=lambda a: (a.level != "red", a.due_at))

    async def last_visits(self, tenant: str) -> dict[str, datetime]:
        """Latest completed visit per active patient."""
        query = (
            select(appointments.c.patient_id, func.max(appointments.c.starts_at))
            .select_from(
                appointments.join(
                    patients,
                    and_(
                        patients.c.tenant == appointments.c.tenant,
                        patients.c.id == appointments.c.patient_id,
                    ),
                )
            )
            .where(
                and_(
                    appointments.c.tenant == tenant,
                    appointments.c.status == "completed",
                    patients.c.restricted.is_(False),
                )
            )
            .group_by(appointments.c.patient_id)
        )
        async with self.db.engine.connect() as conn:
            rows = (await conn.execute(query)).all()
        return {pid: _utc(ts) for pid, ts in rows}

    async def _with_future_appointment(self, tenant: str, now: datetime) -> set[str]:
        query = (
            select(appointments.c.patient_id)
            .where(
                and_(
                    appointments.c.tenant == tenant,
                    appointments.c.starts_at > now,
                    appointments.c.status.in_(("scheduled", "confirmed")),
                )
            )
            .distinct()
        )
        async with self.db.engine.connect() as conn:
            return set((await conn.execute(query)).scalars().all())

    # --- data-subject rights -------------------------------------------------
    async def archive_conversation(
        self,
        tenant: str,
        patient_id: str,
        thread_id: str,
        messages: list[dict[str, Any]],
        review: dict[str, Any] | None,
        reason: str,
    ) -> None:
        """Move a clinical conversation into the clinical record (idempotent: a later
        archive of the same thread replaces the earlier one with the longer history)."""
        values = {
            "patient_id": patient_id,
            "messages": messages,
            "review": review,
            "reason": reason,
            "archived_at": utcnow(),
        }
        key = and_(clinical_notes.c.tenant == tenant, clinical_notes.c.thread_id == thread_id)
        async with self.db.engine.begin() as conn:
            if (
                await conn.execute(update(clinical_notes).where(key).values(**values))
            ).rowcount == 0:
                await conn.execute(
                    insert(clinical_notes).values(tenant=tenant, thread_id=thread_id, **values)
                )
            await self.audit.record_in(
                conn,
                tenant,
                "system:retention",
                "crm.clinical_note.archived",
                f"thread/{thread_id}",
                subject_id=patient_id,
                details={"reason": reason, "messages": len(messages)},
            )

    async def clinical_notes(self, tenant: str, patient_id: str) -> list[dict[str, Any]]:
        query = (
            select(clinical_notes)
            .where(
                and_(clinical_notes.c.tenant == tenant, clinical_notes.c.patient_id == patient_id)
            )
            .order_by(clinical_notes.c.archived_at)
        )
        async with self.db.engine.connect() as conn:
            rows = (await conn.execute(query)).mappings().all()
        return [_row(r) for r in rows]

    async def export_subject(self, tenant: str, patient_id: str) -> dict[str, Any] | None:
        patient = await self._patient(tenant, patient_id)
        notes = await self.clinical_notes(tenant, patient_id)
        if patient is None and not notes:
            return None
        return {
            "patient": patient,
            "appointments": await self.list_appointments(tenant, patient_id=patient_id),
            "treatments": await self.list_treatments(tenant, patient_id=patient_id),
            "clinical_notes": notes,
        }

    async def erase_subject(self, tenant: str, patient_id: str) -> dict[str, Any]:
        if await self._patient(tenant, patient_id) is None:
            return {"patient": "not_found"}
        query = (
            update(patients)
            .where(and_(patients.c.tenant == tenant, patients.c.id == patient_id))
            .values(
                phone=None,
                email=None,
                telegram_chat_id=None,
                preferred_channel=None,
                restricted=True,
                erased_at=utcnow(),
            )
        )
        async with self.db.engine.begin() as conn:
            await conn.execute(query)
        return {"patient": "restricted", "clinical_record": "retained"}
