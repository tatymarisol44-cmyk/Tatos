"""Upgrade from the image-tests revision preserves campaigns and enforces one group."""

import pytest
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import insert, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from orchestrator import migrate
from orchestrator.crm import patients
from orchestrator.db import metadata, utcnow
from orchestrator.marketing_groups import groups, memberships


async def test_upgrade_keeps_existing_campaign_and_enforces_exclusive_membership() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        await migrate.upgrade(engine, "0028_image_scoring")
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO campaigns (tenant,id,name,kind,segment,channel,template,status,"
                    "version,holdout_pct,mode,compliance,created_at,created_by) VALUES "
                    "('acme','old','Campaña anterior','education','dormant','telegram','Hola',"
                    "'draft',1,0,'simulation','{}',:now,'synthetic')"
                ),
                {"now": utcnow()},
            )
        await migrate.upgrade(engine)
        async with engine.begin() as conn:
            old = (
                await conn.execute(
                    text(
                        "SELECT name,audience_groups FROM campaigns "
                        "WHERE tenant='acme' AND id='old'"
                    )
                )
            ).one()
            assert old.name == "Campaña anterior" and old.audience_groups is None
            differences = await conn.run_sync(
                lambda sync: compare_metadata(
                    MigrationContext.configure(sync, opts={"compare_type": True}),
                    metadata,
                )
            )
            assert differences == []
            await conn.execute(
                insert(patients).values(
                    tenant="acme",
                    id="p",
                    display_name="Persona ficticia",
                    restricted=False,
                    created_at=utcnow(),
                )
            )
            for gid in ["a", "b"]:
                await conn.execute(
                    insert(groups).values(
                        tenant="acme",
                        id=gid,
                        name=gid,
                        name_key=gid,
                        version=1,
                        created_at=utcnow(),
                    )
                )
            await conn.execute(
                insert(memberships).values(tenant="acme", group_id="a", patient_id="p")
            )
        with pytest.raises(IntegrityError):
            async with engine.begin() as conn:
                await conn.execute(
                    insert(memberships).values(
                        tenant="acme",
                        group_id="b",
                        patient_id="p",
                    )
                )
    finally:
        await engine.dispose()
