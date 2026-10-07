"""Relational store for governance (audit, reviews, consents) and the CRM.

One SQLAlchemy async Core engine and one `MetaData`, so the same SQL runs on SQLite (dev,
tests: `sqlite+aiosqlite:///:memory:`) and on Postgres in prod
(`postgresql+psycopg://...?sslmode=require`). SQLite (dev, tests) gets its tables from
`create_all`; every other database is versioned with Alembic (orchestrator.migrate): it
is migrated by `agency db upgrade` (or DB_AUTO_MIGRATE) and the service refuses to start
on a schema that is not at the latest revision.

Every table that holds customer data has a `tenant` column and every query filters on it:
tenant isolation is enforced in the data layer, not left to the callers."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import MetaData
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import AsyncAdaptedQueuePool

from orchestrator.config import Settings

metadata = MetaData()

_SECURE_SSLMODES = {"require", "verify-ca", "verify-full"}


def utcnow() -> datetime:
    return datetime.now(UTC)


def aware(value: datetime | None) -> datetime | None:
    """Every timestamp read back, in UTC. SQLite returns naive datetimes (we store UTC);
    Postgres returns them in the session's TimeZone, which on a managed database may be
    local time: the same instant, but a different ISO string, which broke the audit
    chain's hashes there (found on a Postgres in UTC-5)."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def build_engine(settings: Settings) -> AsyncEngine:
    url = make_url(settings.database_url.get_secret_value())
    kwargs: dict[str, Any] = {}
    if url.get_backend_name() == "sqlite":
        if settings.app_env == "prod":
            raise ValueError("DATABASE_URL must point to Postgres in prod, not SQLite")
        # One connection, or every session would see its own empty :memory: db. It is
        # lent exclusively (a pool of one): a second transaction waits for the first to
        # finish instead of interleaving its statements on the same connection, which
        # is what the row locks of Postgres give concurrent code in prod.
        kwargs = {
            "poolclass": AsyncAdaptedQueuePool,
            "pool_size": 1,
            "max_overflow": 0,
            "pool_timeout": 60,
            "connect_args": {"check_same_thread": False},
        }
    elif (
        settings.app_env == "prod"
        and not settings.postgres_allow_insecure
        and url.query.get("sslmode") not in _SECURE_SSLMODES
    ):
        # Patient and customer data must travel encrypted (GDPR Art. 32, HIPAA 164.312).
        raise ValueError("DATABASE_URL needs sslmode=require|verify-ca|verify-full in prod")
    else:
        kwargs = {"pool_size": settings.postgres_pool_size, "pool_pre_ping": True}
    return create_async_engine(url, **kwargs)


class Database:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.engine = build_engine(settings)

    async def start(self) -> None:
        # Imported for their side effect: each module registers its tables on `metadata`.
        from orchestrator import (  # noqa: F401
            auth,
            campaigns,
            clinical_records,
            crm,
            establishment,
            governance,
            inbound,
            knowledge,
            migrate,
            publishing,
            social,
        )

        if self.engine.dialect.name == "sqlite":
            async with self.engine.begin() as conn:
                await conn.run_sync(metadata.create_all)
        elif self.settings.db_auto_migrate:
            await migrate.upgrade(self.engine)
        else:
            await migrate.require_head(self.engine)

    async def close(self) -> None:
        await self.engine.dispose()
