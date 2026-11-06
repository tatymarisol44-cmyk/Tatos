"""Versioned settings and date exceptions used by the single Agenda source."""

from __future__ import annotations

import itertools
from datetime import date, time, timedelta
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import JSON, Column, Date, String, Table, and_, delete, insert, select, update

from orchestrator.db import Database, metadata
from orchestrator.establishment import professionals
from orchestrator.governance import AuditLog

schedule_settings = Table(
    "schedule_settings",
    metadata,
    Column("tenant", String(64), primary_key=True),
    Column("professional_id", String(64), primary_key=True),
    Column("settings", JSON, nullable=False),
)
schedule_exceptions = Table(
    "schedule_exceptions",
    metadata,
    Column("tenant", String(64), primary_key=True),
    Column("professional_id", String(64), primary_key=True),
    Column("day", Date, primary_key=True),
    Column("blocks", JSON, nullable=False),
)


class AvailabilityError(ValueError):
    pass


class DayBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")
    hours: str = Field(pattern=r"^\d{2}:\d{2}-\d{2}:\d{2}$")
    slot_minutes: int = Field(default=50, ge=10, le=240)
    break_minutes: int = Field(default=0, ge=0, le=120)

    @model_validator(mode="after")
    def valid_clock(self) -> DayBlock:
        start, end = (time.fromisoformat(s) for s in self.hours.split("-"))
        if start >= end:
            raise ValueError("the end must be after the start")
        return self


class VisitService(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    id: str = Field(pattern=r"^[\w-]{1,32}$")
    name: str = Field(min_length=1, max_length=100)
    minutes: int = Field(ge=10, le=240)
    break_minutes: int = Field(default=0, ge=0, le=120)
    mode: Literal["in_person", "virtual"] = "in_person"
    weekdays: list[int] = Field(default_factory=lambda: list(range(7)), max_length=7)

    @model_validator(mode="after")
    def valid_days(self) -> VisitService:
        if not self.weekdays or any(d < 0 or d > 6 for d in self.weekdays):
            raise ValueError("weekdays must contain days from 0 to 6")
        return self


class SchedulePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    services: list[VisitService] = Field(default_factory=list, max_length=30)

    @model_validator(mode="after")
    def unique_services(self) -> SchedulePolicy:
        if len({s.id for s in self.services}) != len(self.services):
            raise ValueError("service ids must be unique")
        return self


def validate_blocks(blocks: list[DayBlock]) -> None:
    ordered = sorted(blocks, key=lambda b: b.hours)
    if any(a.hours.split("-")[1] > b.hours.split("-")[0] for a, b in itertools.pairwise(ordered)):
        raise AvailabilityError("blocks overlap")


class Availability:
    def __init__(self, db: Database, audit: AuditLog) -> None:
        self.db, self.audit = db, audit

    @staticmethod
    async def lock(conn: Any, tenant: str, professional_id: str, version: int | None) -> int:
        row = (
            await conn.execute(
                select(professionals.c.schedule_version)
                .where(
                    and_(
                        professionals.c.tenant == tenant,
                        professionals.c.professional_id == professional_id,
                        professionals.c.active.is_(True),
                    )
                )
                .with_for_update()
            )
        ).first()
        if row is None:
            raise KeyError("professional")
        if version is not None and row.schedule_version != version:
            raise AvailabilityError("El horario cambió. Recarga antes de guardar o reservar.")
        return int(row.schedule_version)

    @staticmethod
    async def bump(conn: Any, tenant: str, professional_id: str) -> None:
        await conn.execute(
            update(professionals)
            .where(
                and_(
                    professionals.c.tenant == tenant,
                    professionals.c.professional_id == professional_id,
                )
            )
            .values(schedule_version=professionals.c.schedule_version + 1)
        )

    async def get(self, tenant: str, professional_id: str) -> dict[str, Any]:
        async with self.db.engine.connect() as conn:
            row = (
                await conn.execute(
                    select(professionals.c.schedule_version).where(
                        and_(
                            professionals.c.tenant == tenant,
                            professionals.c.professional_id == professional_id,
                            professionals.c.active.is_(True),
                        )
                    )
                )
            ).first()
            if row is None:
                raise KeyError("professional")
            policy = (
                await conn.execute(
                    select(schedule_settings.c.settings).where(
                        and_(
                            schedule_settings.c.tenant == tenant,
                            schedule_settings.c.professional_id == professional_id,
                        )
                    )
                )
            ).scalar_one_or_none()
            exceptions = (
                await conn.execute(
                    select(schedule_exceptions.c.day, schedule_exceptions.c.blocks)
                    .where(
                        and_(
                            schedule_exceptions.c.tenant == tenant,
                            schedule_exceptions.c.professional_id == professional_id,
                        )
                    )
                    .order_by(schedule_exceptions.c.day)
                )
            ).all()
        return {
            "version": row.schedule_version,
            "policy": policy or SchedulePolicy().model_dump(),
            "exceptions": [
                {"day": day.isoformat(), "blocks": blocks} for day, blocks in exceptions
            ],
        }

    async def policy(
        self,
        tenant: str,
        professional_id: str,
        policy: SchedulePolicy,
        version: int,
        actor: str,
    ) -> dict[str, Any]:
        async with self.db.engine.begin() as conn:
            await self.lock(conn, tenant, professional_id, version)
            await conn.execute(
                delete(schedule_settings).where(
                    and_(
                        schedule_settings.c.tenant == tenant,
                        schedule_settings.c.professional_id == professional_id,
                    )
                )
            )
            await conn.execute(
                insert(schedule_settings).values(
                    tenant=tenant,
                    professional_id=professional_id,
                    settings=policy.model_dump(mode="json"),
                )
            )
            await self.bump(conn, tenant, professional_id)
            await self.audit.record_in(
                conn,
                tenant,
                actor,
                "agenda.policy.updated",
                f"professional/{professional_id}",
                details={"services": len(policy.services)},
            )
        return await self.get(tenant, professional_id)

    async def exception(
        self,
        tenant: str,
        professional_id: str,
        first: date,
        last: date,
        blocks: list[DayBlock] | None,
        version: int,
        actor: str,
    ) -> dict[str, Any]:
        if not 0 <= (last - first).days <= 365:
            raise AvailabilityError("El intervalo debe contener entre 1 y 366 días.")
        if blocks is not None:
            validate_blocks(blocks)
        async with self.db.engine.begin() as conn:
            await self.lock(conn, tenant, professional_id, version)
            await conn.execute(
                delete(schedule_exceptions).where(
                    and_(
                        schedule_exceptions.c.tenant == tenant,
                        schedule_exceptions.c.professional_id == professional_id,
                        schedule_exceptions.c.day >= first,
                        schedule_exceptions.c.day <= last,
                    )
                )
            )
            if blocks is not None:
                await conn.execute(
                    insert(schedule_exceptions),
                    [
                        {
                            "tenant": tenant,
                            "professional_id": professional_id,
                            "day": first + timedelta(days=i),
                            "blocks": [b.model_dump(mode="json") for b in blocks],
                        }
                        for i in range((last - first).days + 1)
                    ],
                )
            await self.bump(conn, tenant, professional_id)
            await self.audit.record_in(
                conn,
                tenant,
                actor,
                "agenda.exceptions.updated",
                f"professional/{professional_id}",
                details={
                    "days": (last - first).days + 1,
                    "mode": "weekly" if blocks is None else "closed" if not blocks else "replace",
                },
            )
        return await self.get(tenant, professional_id)
