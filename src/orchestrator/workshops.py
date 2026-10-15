"""Workshops, talks and events with sign-up from the patient's app (SRS RF-MKT-05).

Experiential marketing for a mental-health practice (Onyeke et al., 2024: immersion,
authenticity, personalisation, community): psychoeducation workshops and talks the
practice organises, open to its patients.

* The practice creates a workshop (title, description, time, place, seats) and may
  cancel it; after it, marks who attended.
* A patient signs up from their app, optionally with companions (the social side of
  engagement, RF-MKT-01), and may withdraw while it has not started. Seats are counted
  with the companions, in the same transaction as the sign-up, so they are never
  exceeded.
* Sign-up may open at a set time (`opens_at`); loyalty tiers open it earlier for their
  members (loyalty.py, RF-MKT-02), the only priority there is.
* A sign-up is a patient's own request: no marketing consent is needed to accept it.
  Inviting patients to a workshop is a campaign, which is (campaigns.py).
* Data class: a workshop holds no patient data (INTERNAL); a registration says that
  someone is a patient of the practice (HEALTH): staff of the practice only, audited."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import (
    Column,
    DateTime,
    Integer,
    String,
    Table,
    Text,
    and_,
    delete,
    func,
    insert,
    select,
    update,
)

from orchestrator.crm import patients
from orchestrator.db import Database, aware, metadata, utcnow
from orchestrator.governance import AuditLog

MAX_COMPANIONS = 3

workshops = Table(
    "workshops",
    metadata,
    Column("tenant", String(64), primary_key=True),
    Column("workshop_id", String(32), primary_key=True),
    Column("title", String(120), nullable=False),
    Column("description", Text, nullable=False),
    Column("starts_at", DateTime(timezone=True), nullable=False),
    Column("duration_min", Integer, nullable=False),
    Column("place", String(200), nullable=False),
    Column("capacity", Integer, nullable=False),
    Column("status", String(16), nullable=False),  # open | cancelled
    Column("opens_at", DateTime(timezone=True), nullable=True),  # null: open at once
    Column("created_by", String(128), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

workshop_registrations = Table(
    "workshop_registrations",
    metadata,
    Column("tenant", String(64), primary_key=True),
    Column("workshop_id", String(32), primary_key=True),
    Column("patient_id", String(64), primary_key=True),
    Column("status", String(16), nullable=False),  # registered | withdrawn | attended | absent
    Column("companions", Integer, nullable=False, default=0, server_default="0"),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

TAKING_A_SEAT = ("registered", "attended")


def _opens(opens_at: datetime | None, early_hours: int) -> datetime | None:
    return opens_at - timedelta(hours=early_hours) if opens_at is not None else None


def _start(row: Any) -> datetime:
    start = aware(row["starts_at"])
    assert start is not None  # the column is NOT NULL
    return start


class WorkshopError(ValueError):
    """A request the workshop refuses (full, cancelled, already started, unknown)."""


class Workshops:
    def __init__(self, db: Database, audit: AuditLog) -> None:
        self.db = db
        self.audit = audit

    # --- the practice ---------------------------------------------------------------------

    async def create(
        self,
        tenant: str,
        actor: str,
        *,
        title: str,
        description: str,
        starts_at: datetime,
        duration_min: int,
        place: str,
        capacity: int,
        opens_at: datetime | None = None,
    ) -> dict[str, Any]:
        if starts_at <= utcnow():
            raise WorkshopError("a workshop is scheduled in the future")
        if opens_at is not None and opens_at >= starts_at:
            raise WorkshopError("sign-up must open before the workshop starts")
        workshop_id = uuid.uuid4().hex[:16]
        async with self.db.engine.begin() as conn:
            await conn.execute(
                insert(workshops).values(
                    tenant=tenant,
                    workshop_id=workshop_id,
                    title=title,
                    description=description,
                    starts_at=starts_at,
                    duration_min=duration_min,
                    place=place,
                    capacity=capacity,
                    status="open",
                    opens_at=opens_at,
                    created_by=actor,
                    created_at=utcnow(),
                )
            )
            await self.audit.record_in(
                conn, tenant, actor, "workshop.created", f"workshop/{workshop_id}"
            )
        return await self.get(tenant, workshop_id)

    async def listing(self, tenant: str, include_past: bool = False) -> list[dict[str, Any]]:
        query = select(workshops).where(workshops.c.tenant == tenant)
        if not include_past:
            query = query.where(workshops.c.starts_at >= utcnow() - timedelta(days=1))
        query = query.order_by(workshops.c.starts_at)
        async with self.db.engine.connect() as conn:
            rows = (await conn.execute(query)).mappings().all()
            taken = await self._taken(conn, tenant)
        return [self._public(r, taken.get(r["workshop_id"], 0)) for r in rows]

    async def get(self, tenant: str, workshop_id: str) -> dict[str, Any]:
        async with self.db.engine.connect() as conn:
            row = await self._row(conn, tenant, workshop_id)
            taken = await self._taken(conn, tenant, workshop_id)
        return self._public(row, taken.get(workshop_id, 0))

    async def registrations(
        self, tenant: str, workshop_id: str, actor: str
    ) -> list[dict[str, Any]]:
        """Who signed up (names), for the practice's staff; the read is audited."""
        query = (
            select(workshop_registrations, patients.c.display_name)
            .join(
                patients,
                and_(
                    patients.c.tenant == workshop_registrations.c.tenant,
                    patients.c.id == workshop_registrations.c.patient_id,
                ),
            )
            .where(
                and_(
                    workshop_registrations.c.tenant == tenant,
                    workshop_registrations.c.workshop_id == workshop_id,
                    workshop_registrations.c.status != "withdrawn",
                )
            )
            .order_by(patients.c.display_name)
        )
        async with self.db.engine.connect() as conn:
            await self._row(conn, tenant, workshop_id)
            rows = (await conn.execute(query)).mappings().all()
        await self.audit.record(
            tenant, actor, "workshop.registrations_read", f"workshop/{workshop_id}"
        )
        return [
            {
                "patient_id": r["patient_id"],
                "name": r["display_name"],
                "status": r["status"],
                "companions": r["companions"],
                "registered_at": aware(r["created_at"]),
            }
            for r in rows
        ]

    async def cancel(self, tenant: str, workshop_id: str, actor: str) -> None:
        async with self.db.engine.begin() as conn:
            await self._row(conn, tenant, workshop_id)
            await conn.execute(
                update(workshops)
                .where(and_(workshops.c.tenant == tenant, workshops.c.workshop_id == workshop_id))
                .values(status="cancelled")
            )
            await self.audit.record_in(
                conn, tenant, actor, "workshop.cancelled", f"workshop/{workshop_id}"
            )

    async def mark(
        self, tenant: str, workshop_id: str, patient_id: str, attended: bool, actor: str
    ) -> None:
        """After the workshop: who came (it feeds the engagement index, RF-MKT-01)."""
        async with self.db.engine.begin() as conn:
            row = await self._row(conn, tenant, workshop_id)
            if _start(row) > utcnow():
                raise WorkshopError("attendance is marked once the workshop has started")
            done = await conn.execute(
                update(workshop_registrations)
                .where(
                    and_(
                        workshop_registrations.c.tenant == tenant,
                        workshop_registrations.c.workshop_id == workshop_id,
                        workshop_registrations.c.patient_id == patient_id,
                        workshop_registrations.c.status != "withdrawn",
                    )
                )
                .values(status="attended" if attended else "absent", updated_at=utcnow())
            )
            if not done.rowcount:
                raise KeyError(patient_id)
            await self.audit.record_in(
                conn,
                tenant,
                actor,
                "workshop.attendance",
                f"workshop/{workshop_id}",
                subject_id=patient_id,
                details={"attended": attended},
            )

    # --- the patient ----------------------------------------------------------------------

    async def for_patient(
        self, tenant: str, patient_id: str, early_hours: int = 0
    ) -> list[dict[str, Any]]:
        """Upcoming open workshops, each with the patient's own sign-up and when sign-up
        opens for them (earlier with a loyalty tier)."""
        upcoming = [
            w
            for w in await self.listing(tenant)
            if w["status"] == "open" and w["starts_at"] > utcnow()
        ]
        query = select(workshop_registrations).where(
            and_(
                workshop_registrations.c.tenant == tenant,
                workshop_registrations.c.patient_id == patient_id,
                workshop_registrations.c.status != "withdrawn",
            )
        )
        async with self.db.engine.connect() as conn:
            mine = {r["workshop_id"]: r for r in (await conn.execute(query)).mappings()}
        return [
            {
                **w,
                "opens_for_you": _opens(w["opens_at"], early_hours),
                "registered": w["workshop_id"] in mine,
                "companions": mine[w["workshop_id"]]["companions"]
                if w["workshop_id"] in mine
                else 0,
            }
            for w in upcoming
        ]

    async def register(
        self,
        tenant: str,
        workshop_id: str,
        patient_id: str,
        companions: int,
        early_hours: int = 0,
    ) -> dict[str, Any]:
        if not 0 <= companions <= MAX_COMPANIONS:
            raise WorkshopError(f"up to {MAX_COMPANIONS} companions")
        now = utcnow()
        async with self.db.engine.begin() as conn:
            row = await self._row(conn, tenant, workshop_id, lock=True)
            if row["status"] != "open" or _start(row) <= now:
                raise WorkshopError("this workshop is not open for sign-up")
            opens = _opens(aware(row["opens_at"]), early_hours)
            if opens is not None and now < opens:
                raise WorkshopError(f"sign-up opens at {opens.isoformat()}")
            mine = and_(
                workshop_registrations.c.tenant == tenant,
                workshop_registrations.c.workshop_id == workshop_id,
                workshop_registrations.c.patient_id == patient_id,
            )
            current = (
                (await conn.execute(select(workshop_registrations).where(mine))).mappings().first()
            )
            already = (
                current["companions"] + 1
                if current is not None and current["status"] in TAKING_A_SEAT
                else 0
            )
            taken = (await self._taken(conn, tenant, workshop_id)).get(workshop_id, 0)
            if taken - already + companions + 1 > row["capacity"]:
                raise WorkshopError("there are not enough seats left")
            if current is None:
                await conn.execute(
                    insert(workshop_registrations).values(
                        tenant=tenant,
                        workshop_id=workshop_id,
                        patient_id=patient_id,
                        status="registered",
                        companions=companions,
                        created_at=now,
                        updated_at=now,
                    )
                )
            else:
                await conn.execute(
                    update(workshop_registrations)
                    .where(mine)
                    .values(status="registered", companions=companions, updated_at=now)
                )
            await self.audit.record_in(
                conn,
                tenant,
                f"patient:{patient_id}",
                "workshop.registered",
                f"workshop/{workshop_id}",
                subject_id=patient_id,
                details={"companions": companions},
            )
        return await self.get(tenant, workshop_id)

    async def withdraw(self, tenant: str, workshop_id: str, patient_id: str) -> bool:
        async with self.db.engine.begin() as conn:
            row = await self._row(conn, tenant, workshop_id)
            if _start(row) <= utcnow():
                raise WorkshopError("the workshop has already started")
            done = await conn.execute(
                update(workshop_registrations)
                .where(
                    and_(
                        workshop_registrations.c.tenant == tenant,
                        workshop_registrations.c.workshop_id == workshop_id,
                        workshop_registrations.c.patient_id == patient_id,
                        workshop_registrations.c.status == "registered",
                    )
                )
                .values(status="withdrawn", updated_at=utcnow())
            )
            if done.rowcount:
                await self.audit.record_in(
                    conn,
                    tenant,
                    f"patient:{patient_id}",
                    "workshop.withdrawn",
                    f"workshop/{workshop_id}",
                    subject_id=patient_id,
                )
        return bool(done.rowcount)

    # --- the patient's rights and the engagement index ---------------------------------------

    async def history(self, tenant: str, patient_id: str) -> list[dict[str, Any]]:
        """Every sign-up of the patient, for an access request and the engagement index."""
        query = (
            select(workshop_registrations, workshops.c.title, workshops.c.starts_at)
            .join(
                workshops,
                and_(
                    workshops.c.tenant == workshop_registrations.c.tenant,
                    workshops.c.workshop_id == workshop_registrations.c.workshop_id,
                ),
            )
            .where(
                and_(
                    workshop_registrations.c.tenant == tenant,
                    workshop_registrations.c.patient_id == patient_id,
                )
            )
        )
        async with self.db.engine.connect() as conn:
            rows = (await conn.execute(query)).mappings().all()
        return [
            {
                "workshop": r["title"],
                "starts_at": aware(r["starts_at"]),
                "status": r["status"],
                "companions": r["companions"],
            }
            for r in rows
        ]

    async def erase_subject(self, tenant: str, patient_id: str) -> int:
        """Erasure: the patient's sign-ups go (a workshop is not part of the record)."""
        async with self.db.engine.begin() as conn:
            done = await conn.execute(
                delete(workshop_registrations).where(
                    and_(
                        workshop_registrations.c.tenant == tenant,
                        workshop_registrations.c.patient_id == patient_id,
                    )
                )
            )
        return int(done.rowcount or 0)

    # --- internals ----------------------------------------------------------------------------

    async def _row(self, conn: Any, tenant: str, workshop_id: str, lock: bool = False) -> Any:
        query = select(workshops).where(
            and_(workshops.c.tenant == tenant, workshops.c.workshop_id == workshop_id)
        )
        if lock:  # Postgres: one sign-up at a time per workshop, so seats hold
            query = query.with_for_update()
        row = (await conn.execute(query)).mappings().first()
        if row is None:
            raise KeyError(workshop_id)
        return row

    @staticmethod
    async def _taken(conn: Any, tenant: str, workshop_id: str | None = None) -> dict[str, int]:
        """Seats taken per workshop: each sign-up and its companions."""
        query = (
            select(
                workshop_registrations.c.workshop_id,
                func.sum(workshop_registrations.c.companions + 1),
            )
            .where(
                and_(
                    workshop_registrations.c.tenant == tenant,
                    workshop_registrations.c.status.in_(TAKING_A_SEAT),
                )
            )
            .group_by(workshop_registrations.c.workshop_id)
        )
        if workshop_id is not None:
            query = query.where(workshop_registrations.c.workshop_id == workshop_id)
        return {r[0]: int(r[1] or 0) for r in await conn.execute(query)}

    @staticmethod
    def _public(row: Any, taken: int) -> dict[str, Any]:
        return {
            "workshop_id": row["workshop_id"],
            "title": row["title"],
            "description": row["description"],
            "starts_at": aware(row["starts_at"]),
            "duration_min": row["duration_min"],
            "place": row["place"],
            "capacity": row["capacity"],
            "seats_left": max(0, row["capacity"] - taken),
            "status": row["status"],
            "opens_at": aware(row["opens_at"]),
        }
