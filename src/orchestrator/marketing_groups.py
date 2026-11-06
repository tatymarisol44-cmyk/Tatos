"""Manually selected marketing audiences, independent of clinical data and scoring."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import (
    Column,
    DateTime,
    ForeignKeyConstraint,
    Integer,
    String,
    Table,
    UniqueConstraint,
    and_,
    delete,
    func,
    insert,
    select,
    update,
)
from sqlalchemy.exc import IntegrityError

from orchestrator.crm import patients
from orchestrator.db import Database, metadata, utcnow
from orchestrator.governance import AuditLog, ConsentRegistry, Purpose

groups = Table(
    "marketing_groups",
    metadata,
    Column("tenant", String(64), primary_key=True),
    Column("id", String(64), primary_key=True),
    Column("name", String(100), nullable=False),
    Column("name_key", String(200), nullable=False),
    Column("version", Integer, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("tenant", "name_key", name="uq_marketing_group_name"),
)
memberships = Table(
    "marketing_group_members",
    metadata,
    Column("tenant", String(64), primary_key=True),
    Column("group_id", String(64), primary_key=True),
    Column("patient_id", String(64), primary_key=True),
    ForeignKeyConstraint(
        ["tenant", "group_id"],
        ["marketing_groups.tenant", "marketing_groups.id"],
        ondelete="CASCADE",
    ),
    ForeignKeyConstraint(
        ["tenant", "patient_id"],
        ["crm_patients.tenant", "crm_patients.id"],
        ondelete="CASCADE",
    ),
    UniqueConstraint("tenant", "patient_id", name="uq_marketing_person_one_group"),
)


class GroupError(ValueError):
    pass


class GroupConflict(GroupError):
    pass


class MarketingGroups:
    def __init__(self, db: Database, audit: AuditLog, consents: ConsentRegistry) -> None:
        self.db, self.audit, self.consents = db, audit, consents

    @staticmethod
    def _where(tenant: str, group_id: str) -> Any:
        return and_(groups.c.tenant == tenant, groups.c.id == group_id)

    async def list_all(self, tenant: str) -> list[dict[str, Any]]:
        counts = (
            select(memberships.c.group_id, func.count().label("member_count"))
            .select_from(
                memberships.join(
                    patients,
                    and_(
                        patients.c.tenant == memberships.c.tenant,
                        patients.c.id == memberships.c.patient_id,
                    ),
                )
            )
            .where(and_(memberships.c.tenant == tenant, patients.c.restricted.is_(False)))
            .group_by(memberships.c.group_id)
            .subquery()
        )
        query = select(
            groups.c.id,
            groups.c.name,
            groups.c.version,
            func.coalesce(counts.c.member_count, 0).label("member_count"),
        ).select_from(groups.outerjoin(counts, counts.c.group_id == groups.c.id))
        async with self.db.engine.connect() as conn:
            rows = (
                (
                    await conn.execute(
                        query.where(groups.c.tenant == tenant).order_by(groups.c.name_key)
                    )
                )
                .mappings()
                .all()
            )
        return [dict(row) for row in rows]

    async def get(self, tenant: str, group_id: str) -> dict[str, Any]:
        async with self.db.engine.connect() as conn:
            group = (
                (
                    await conn.execute(
                        select(
                            groups.c.id,
                            groups.c.name,
                            groups.c.version,
                        ).where(self._where(tenant, group_id))
                    )
                )
                .mappings()
                .first()
            )
            if group is None:
                raise KeyError("group")
            ids = (
                (
                    await conn.execute(
                        select(memberships.c.patient_id)
                        .join(
                            patients,
                            and_(
                                patients.c.tenant == memberships.c.tenant,
                                patients.c.id == memberships.c.patient_id,
                            ),
                        )
                        .where(
                            and_(
                                memberships.c.tenant == tenant,
                                memberships.c.group_id == group_id,
                                patients.c.restricted.is_(False),
                            )
                        )
                        .order_by(memberships.c.patient_id)
                    )
                )
                .scalars()
                .all()
            )
        return {**dict(group), "member_ids": list(ids), "member_count": len(ids)}

    async def save(
        self,
        tenant: str,
        name: str,
        member_ids: list[str],
        actor: str,
        *,
        group_id: str | None = None,
        version: int | None = None,
    ) -> dict[str, Any]:
        name = name.strip()
        if not name or len(name) > 100:
            raise GroupError("Escribe un nombre de grupo de hasta 100 caracteres.")
        ids = sorted(set(member_ids))
        if len(ids) > 5000:
            raise GroupError("Un grupo admite hasta 5000 personas.")
        creating = group_id is None
        gid = group_id or uuid.uuid4().hex[:12]
        try:
            async with self.db.engine.begin() as conn:
                if not creating:
                    row = (
                        await conn.execute(
                            select(groups.c.version)
                            .where(self._where(tenant, gid))
                            .with_for_update()
                        )
                    ).first()
                    if row is None:
                        raise KeyError("group")
                    if row.version != version:
                        raise GroupConflict("El grupo cambió. Ábrelo otra vez antes de guardar.")
                if ids:
                    available = (
                        (
                            await conn.execute(
                                select(patients.c.id)
                                .where(
                                    and_(
                                        patients.c.tenant == tenant,
                                        patients.c.id.in_(ids),
                                        patients.c.restricted.is_(False),
                                    )
                                )
                                .with_for_update()
                            )
                        )
                        .scalars()
                        .all()
                    )
                    if len(available) != len(ids):
                        raise GroupError("Alguna persona no está disponible en este consultorio.")
                    assigned_elsewhere = (
                        await conn.execute(
                            select(memberships.c.patient_id)
                            .where(
                                and_(
                                    memberships.c.tenant == tenant,
                                    memberships.c.patient_id.in_(ids),
                                    memberships.c.group_id != gid,
                                )
                            )
                            .limit(1)
                        )
                    ).first()
                    if assigned_elsewhere:
                        raise GroupConflict(
                            "Una persona ya pertenece a otro grupo. "
                            "Quítala de ese grupo antes de moverla."
                        )
                if creating:
                    await conn.execute(
                        insert(groups).values(
                            tenant=tenant,
                            id=gid,
                            name=name,
                            name_key=name.casefold(),
                            version=1,
                            created_at=utcnow(),
                        )
                    )
                else:
                    changed = await conn.execute(
                        update(groups)
                        .where(
                            and_(
                                self._where(tenant, gid),
                                groups.c.version == version,
                            )
                        )
                        .values(name=name, name_key=name.casefold(), version=groups.c.version + 1)
                    )
                    if not changed.rowcount:
                        raise GroupConflict("El grupo cambió. Ábrelo otra vez antes de guardar.")
                    await conn.execute(
                        delete(memberships).where(
                            and_(
                                memberships.c.tenant == tenant,
                                memberships.c.group_id == gid,
                            )
                        )
                    )
                if ids:
                    await conn.execute(
                        insert(memberships),
                        [{"tenant": tenant, "group_id": gid, "patient_id": pid} for pid in ids],
                    )
                await self.audit.record_in(
                    conn,
                    tenant,
                    actor,
                    "marketing.group.created" if creating else "marketing.group.updated",
                    f"marketing_group/{gid}",
                    details={"member_count": len(ids)},
                )
        except IntegrityError:
            raise GroupConflict(
                "El nombre o la asignación de personas cambió. Recarga los grupos."
            ) from None
        return await self.get(tenant, gid)

    async def remove(self, tenant: str, group_id: str, version: int, actor: str) -> None:
        async with self.db.engine.begin() as conn:
            row = (
                await conn.execute(
                    select(groups.c.version).where(self._where(tenant, group_id)).with_for_update()
                )
            ).first()
            if row is None:
                raise KeyError("group")
            if row.version != version:
                raise GroupConflict("El grupo cambió. Ábrelo otra vez antes de eliminarlo.")
            await conn.execute(
                delete(memberships).where(
                    and_(
                        memberships.c.tenant == tenant,
                        memberships.c.group_id == group_id,
                    )
                )
            )
            changed = await conn.execute(
                delete(groups).where(
                    and_(
                        self._where(tenant, group_id),
                        groups.c.version == version,
                    )
                )
            )
            if not changed.rowcount:
                raise GroupConflict("El grupo cambió; recarga la lista.")
            await self.audit.record_in(
                conn, tenant, actor, "marketing.group.deleted", f"marketing_group/{group_id}"
            )

    async def contacts(self, tenant: str, search: str, offset: int, limit: int) -> dict[str, Any]:
        condition = and_(patients.c.tenant == tenant, patients.c.restricted.is_(False))
        query = select(patients.c.id, patients.c.display_name).where(condition)
        if search:
            query = query.where(patients.c.display_name.icontains(search, autoescape=True))
        async with self.db.engine.connect() as conn:
            total = (
                await conn.execute(select(func.count()).select_from(query.subquery()))
            ).scalar_one()
            rows = (
                (
                    await conn.execute(
                        query.order_by(patients.c.display_name, patients.c.id)
                        .offset(offset)
                        .limit(limit)
                    )
                )
                .mappings()
                .all()
            )
            assignments = (
                await conn.execute(
                    select(
                        memberships.c.patient_id,
                        memberships.c.group_id,
                    ).where(
                        and_(
                            memberships.c.tenant == tenant,
                            memberships.c.patient_id.in_([r["id"] for r in rows]),
                        )
                    )
                )
            ).all()
        consent = await self.consents.granted_subjects(tenant, Purpose.MARKETING)
        by_person: dict[str, list[str]] = {}
        for pid, gid in assignments:
            by_person.setdefault(pid, []).append(gid)
        return {
            "total": total,
            "contacts": [
                {
                    **dict(r),
                    "group_ids": by_person.get(r["id"], []),
                    "marketing_consent": r["id"] in consent,
                }
                for r in rows
            ],
        }

    async def audience(
        self, tenant: str, group_ids: list[str]
    ) -> tuple[list[str], list[dict[str, str]]]:
        ids = sorted(set(group_ids))
        if not ids or len(ids) > 100:
            raise GroupError("Selecciona entre 1 y 100 grupos.")
        async with self.db.engine.connect() as conn:
            selected = (
                (
                    await conn.execute(
                        select(groups.c.id, groups.c.name)
                        .where(and_(groups.c.tenant == tenant, groups.c.id.in_(ids)))
                        .order_by(groups.c.name_key)
                    )
                )
                .mappings()
                .all()
            )
            if len(selected) != len(ids):
                raise GroupError("Algún grupo no está disponible en este consultorio.")
            members = (
                (
                    await conn.execute(
                        select(memberships.c.patient_id)
                        .distinct()
                        .join(
                            patients,
                            and_(
                                patients.c.tenant == memberships.c.tenant,
                                patients.c.id == memberships.c.patient_id,
                            ),
                        )
                        .where(
                            and_(
                                memberships.c.tenant == tenant,
                                memberships.c.group_id.in_(ids),
                                patients.c.restricted.is_(False),
                            )
                        )
                        .order_by(memberships.c.patient_id)
                    )
                )
                .scalars()
                .all()
            )
        return list(members), [dict(r) for r in selected]

    async def export_subject(self, tenant: str, patient_id: str) -> list[dict[str, Any]]:
        async with self.db.engine.connect() as conn:
            rows = (
                (
                    await conn.execute(
                        select(groups.c.id, groups.c.name)
                        .join(
                            memberships,
                            and_(
                                memberships.c.tenant == groups.c.tenant,
                                memberships.c.group_id == groups.c.id,
                            ),
                        )
                        .where(
                            and_(
                                memberships.c.tenant == tenant,
                                memberships.c.patient_id == patient_id,
                            )
                        )
                    )
                )
                .mappings()
                .all()
            )
        return [dict(r) for r in rows]

    async def erase_subject(self, tenant: str, patient_id: str) -> int:
        async with self.db.engine.begin() as conn:
            # Invalidate any membership editor opened before the erasure.
            affected = select(memberships.c.group_id).where(
                and_(
                    memberships.c.tenant == tenant,
                    memberships.c.patient_id == patient_id,
                )
            )
            await conn.execute(
                update(groups)
                .where(
                    and_(
                        groups.c.tenant == tenant,
                        groups.c.id.in_(affected),
                    )
                )
                .values(version=groups.c.version + 1)
            )
            result = await conn.execute(
                delete(memberships).where(
                    and_(
                        memberships.c.tenant == tenant,
                        memberships.c.patient_id == patient_id,
                    )
                )
            )
        return result.rowcount
