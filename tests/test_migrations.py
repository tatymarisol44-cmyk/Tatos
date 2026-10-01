"""Versioned migrations (audit finding A33): the migrations build exactly the models'
schema, an older database upgrades without losing data, and the service refuses to run
on an outdated schema. On a SQLite file always; on a fresh Postgres database per test
when TEST_POSTGRES_URL is set."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from pydantic import SecretStr
from sqlalchemy import insert, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from orchestrator import migrate
from orchestrator.config import Settings
from orchestrator.db import Database, metadata, utcnow
from orchestrator.governance import AuditLog

POSTGRES_URL = os.environ.get("TEST_POSTGRES_URL")


def _backends() -> list[str]:
    return ["sqlite", "postgres"] if POSTGRES_URL else ["sqlite"]


@pytest.fixture
def database_url(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[str]:
    if request.param == "sqlite":
        yield f"sqlite+aiosqlite:///{tmp_path / 'agency.db'}"
        return
    import psycopg

    assert POSTGRES_URL is not None
    name = f"mig_{uuid.uuid4().hex[:10]}"
    admin = POSTGRES_URL.rsplit("/", 1)[0] + "/postgres"
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(f"CREATE DATABASE {name}")
    try:
        yield (
            POSTGRES_URL.rsplit("/", 1)[0].replace("postgresql://", "postgresql+psycopg://")
            + f"/{name}"
        )
    finally:
        with psycopg.connect(admin, autocommit=True) as conn:
            conn.execute(f"DROP DATABASE {name} WITH (FORCE)")


@pytest.fixture
async def engine(database_url: str) -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine(database_url)
    yield eng
    await eng.dispose()


pytestmark = pytest.mark.parametrize("database_url", _backends(), indirect=True)


def _diff(sync_conn: Any) -> list[Any]:
    from orchestrator import auth, campaigns, crm, governance, knowledge  # noqa: F401

    ctx = MigrationContext.configure(sync_conn, opts={"compare_type": True})
    return [
        d
        for d in compare_metadata(ctx, metadata)
        if d[0] != "remove_table" or d[1].name != "alembic_version"
    ]


async def test_migrations_build_the_models_schema(engine: AsyncEngine) -> None:
    await migrate.upgrade(engine)
    assert await migrate.current(engine) == migrate.head()
    async with engine.connect() as conn:
        assert await conn.run_sync(_diff) == []  # nothing in the models is missing


async def test_upgrading_an_older_database_keeps_its_data(engine: AsyncEngine) -> None:
    # A database of the version the auditors reviewed (cfd0c85, revision 0001)...
    await migrate.upgrade(engine, "0001")
    audit = AuditLog(SimpleNamespace(engine=engine))  # type: ignore[arg-type]
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO campaigns (tenant, id, name, kind, segment, channel, template, "
                "status, version, holdout_pct, compliance, created_at, created_by) VALUES "
                "('acme', 'c1', 'Vuelve', 'recall', 'dormant', 'telegram', 'Hola', "
                "'completed', 1, 10, '{}', :ts, 'ana')"
            ),
            {"ts": utcnow() - timedelta(days=30)},
        )
    await audit.record("acme", "ana", "campaign.created", "campaign/c1")
    # ...upgraded to this version.
    await migrate.upgrade(engine)
    async with engine.connect() as conn:
        row = (await conn.execute(text("SELECT name, mode, population FROM campaigns"))).one()
    assert (row.name, row.mode, row.population) == ("Vuelve", "live", None)
    assert (await audit.verify("acme"))["ok"] is True
    from orchestrator.knowledge import knowledge_documents

    async with engine.begin() as conn:  # the new tables are there and usable
        await conn.execute(
            insert(knowledge_documents).values(
                tenant="acme", doc_id="d", version="v", title="t", chunks=1, updated_at=utcnow()
            )
        )
        assert (await conn.execute(select(knowledge_documents.c.doc_id))).scalar_one() == "d"


async def test_service_refuses_an_outdated_schema(
    engine: AsyncEngine, settings: Settings, database_url: str
) -> None:
    await migrate.upgrade(engine, "0001")
    with pytest.raises(migrate.SchemaOutOfDateError, match="run `agency db upgrade`"):
        await migrate.require_head(engine)
    if engine.dialect.name == "postgresql":  # SQLite always builds its tables directly
        settings.database_url = SecretStr(database_url)
        db = Database(settings)
        with pytest.raises(migrate.SchemaOutOfDateError):
            await db.start()
        settings.db_auto_migrate = True
        await Database(settings).start()  # or migrates itself when allowed to
        assert await migrate.current(engine) == migrate.head()


def _together(code: str, env: dict[str, str]) -> list[int]:
    procs = [subprocess.Popen([sys.executable, "-c", code], env=env) for _ in range(2)]
    return [p.wait(timeout=120) for p in procs]


async def test_replicas_migrating_together_do_not_collide(
    engine: AsyncEngine, database_url: str
) -> None:
    if engine.dialect.name != "postgresql":
        pytest.skip("the advisory lock is a Postgres feature; SQLite is single-process dev")
    # Two pods are two processes: start two `agency db upgrade` at the same moment.
    code = "from orchestrator.cli import main; raise SystemExit(main(['db', 'upgrade']))"
    env = {**os.environ, "DATABASE_URL": database_url, "APP_ENV": "dev"}
    assert await asyncio.to_thread(_together, code, env) == [0, 0]
    assert await migrate.current(engine) == migrate.head()
