"""Relational store for governance (audit, reviews, consents) and the CRM.

One SQLAlchemy async Core engine and one `MetaData`, so the same SQL runs on SQLite (dev,
tests: `sqlite+aiosqlite:///:memory:`) and on Postgres in prod
(`postgresql+psycopg://...?sslmode=require`). Tables are created at startup with
`create_all` (idempotent); schema migrations are out of scope for this version.

Every table that holds customer data has a `tenant` column and every query filters on it:
tenant isolation is enforced in the data layer, not left to the callers."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import MetaData
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import StaticPool

from orchestrator.config import Settings

metadata = MetaData()

_SECURE_SSLMODES = {"require", "verify-ca", "verify-full"}


def utcnow() -> datetime:
    return datetime.now(UTC)


def aware(value: datetime | None) -> datetime | None:
    """SQLite returns naive datetimes for timezone-aware columns; every timestamp we
    store is UTC, so a naive value read back is UTC too."""
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


def build_engine(settings: Settings) -> AsyncEngine:
    url = make_url(settings.database_url.get_secret_value())
    kwargs: dict[str, Any] = {}
    if url.get_backend_name() == "sqlite":
        if settings.app_env == "prod":
            raise ValueError("DATABASE_URL must point to Postgres in prod, not SQLite")
        # One shared connection, or every session would see its own empty :memory: db.
        kwargs = {"poolclass": StaticPool, "connect_args": {"check_same_thread": False}}
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
        self.engine = build_engine(settings)

    async def start(self) -> None:
        # Imported for their side effect: each module registers its tables on `metadata`.
        from orchestrator import campaigns, crm, governance  # noqa: F401

        async with self.engine.begin() as conn:
            await conn.run_sync(metadata.create_all)

    async def close(self) -> None:
        await self.engine.dispose()
