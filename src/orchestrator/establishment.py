"""An establishment (the tenant) with several professionals (ADR 0014, rule 5).

A psychology centre may have a psychologist and a psychiatrist: each professional has their
own profession pack, so each one's documents, prescriptions and marketing rules are those of
their profession. Things that belong to the establishment stay on the tenant: the ACESS
special-prescription blocks are bought by the establishment's legal representative
(ACESS-2022-0046 Arts. 8 and 12), and the tenant's pack still applies to anything that is
not tied to one professional (for example the practice's own social accounts)."""

from __future__ import annotations

from typing import Any

from sqlalchemy import Boolean, Column, DateTime, String, Table, and_, insert, select, update

from orchestrator.config import Settings
from orchestrator.db import Database, metadata, utcnow
from orchestrator.governance import AuditLog
from orchestrator.packs import Pack, load_packs, pack_for

professionals = Table(
    "professionals",
    metadata,
    Column("tenant", String(64), primary_key=True),
    Column("professional_id", String(64), primary_key=True),
    Column("display_name", String(120), nullable=False),
    Column("pack_id", String(64), nullable=False),
    # The staff key this professional signs in with, if any (principals.id).
    Column("staff_id", String(128), nullable=True),
    Column("active", Boolean, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("created_by", String(128), nullable=False),
)


class ProfessionalError(ValueError):
    """Unknown or abstract pack, duplicate id, or an inactive professional."""


def _row(row: Any) -> dict[str, Any]:
    return {c.name: getattr(row, c.name) for c in professionals.c if c.name != "tenant"}


class Professionals:
    def __init__(self, db: Database, audit: AuditLog, settings: Settings) -> None:
        self.db = db
        self.audit = audit
        self.settings = settings

    async def add(
        self,
        tenant: str,
        actor: str,
        *,
        professional_id: str,
        display_name: str,
        pack_id: str,
        staff_id: str | None = None,
    ) -> dict[str, Any]:
        packs = load_packs()
        if pack_id not in packs or packs[pack_id].abstract:
            raise ProfessionalError(f"unknown or abstract pack {pack_id!r}")
        async with self.db.engine.begin() as conn:
            existing = await conn.execute(
                select(professionals.c.professional_id).where(
                    and_(
                        professionals.c.tenant == tenant,
                        professionals.c.professional_id == professional_id,
                    )
                )
            )
            if existing.first() is not None:
                raise ProfessionalError(f"professional {professional_id!r} already exists")
            await conn.execute(
                insert(professionals).values(
                    tenant=tenant,
                    professional_id=professional_id,
                    display_name=display_name,
                    pack_id=pack_id,
                    staff_id=staff_id,
                    active=True,
                    created_at=utcnow(),
                    created_by=actor,
                )
            )
            await self.audit.record_in(
                conn,
                tenant,
                actor,
                "professional.added",
                f"professional/{professional_id}",
                details={"pack": pack_id},
            )
        return await self.get(tenant, professional_id)

    async def get(self, tenant: str, professional_id: str) -> dict[str, Any]:
        query = select(professionals).where(
            and_(
                professionals.c.tenant == tenant, professionals.c.professional_id == professional_id
            )
        )
        async with self.db.engine.connect() as conn:
            row = (await conn.execute(query)).first()
        if row is None:
            raise KeyError(professional_id)
        return _row(row)

    async def list(self, tenant: str) -> list[dict[str, Any]]:
        query = (
            select(professionals)
            .where(professionals.c.tenant == tenant)
            .order_by(professionals.c.professional_id)
        )
        async with self.db.engine.connect() as conn:
            return [_row(r) for r in await conn.execute(query)]

    async def disable(self, tenant: str, professional_id: str, actor: str) -> bool:
        query = (
            update(professionals)
            .where(
                and_(
                    professionals.c.tenant == tenant,
                    professionals.c.professional_id == professional_id,
                    professionals.c.active.is_(True),
                )
            )
            .values(active=False)
        )
        async with self.db.engine.begin() as conn:
            done = (await conn.execute(query)).rowcount == 1
            if done:
                await self.audit.record_in(
                    conn, tenant, actor, "professional.disabled", f"professional/{professional_id}"
                )
        return done

    async def require_active(self, tenant: str, professional_id: str) -> dict[str, Any]:
        try:
            found = await self.get(tenant, professional_id)
        except KeyError as exc:
            raise ProfessionalError(f"unknown professional {professional_id!r}") from exc
        if not found["active"]:
            raise ProfessionalError(f"professional {professional_id!r} is disabled")
        return found

    async def pack_for(self, tenant: str, professional_id: str | None) -> Pack:
        """The professional's pack, or the establishment's when nobody in particular."""
        if professional_id is None:
            return pack_for(self.settings, tenant)
        found = await self.require_active(tenant, professional_id)
        return load_packs()[found["pack_id"]]
