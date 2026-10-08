"""Versioned schema migrations (Alembic), audit finding A33.

- Development and tests on SQLite create the tables directly (fast, throw-away).
- Every other database is migrated with `agency db upgrade` (a Kubernetes init
  container runs it before the API starts), and the service refuses to start on a
  schema that is not at the latest revision, instead of guessing.

Migrations live in `orchestrator/migrations/versions`. To add one after changing a
table: `agency db revision -m "what changed"` against a database at head, then review
the generated file."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy.ext.asyncio import AsyncEngine

MIGRATIONS = Path(__file__).parent / "migrations"


class SchemaOutOfDateError(RuntimeError):
    """The database schema is not at the revision this code expects."""


def _config(connection: Any = None) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS))
    cfg.attributes["connection"] = connection
    return cfg


def head() -> str:
    revision = ScriptDirectory.from_config(_config()).get_current_head()
    assert revision is not None
    return revision


async def upgrade(engine: AsyncEngine, revision: str = "head") -> None:
    async with engine.begin() as conn:
        if conn.dialect.name == "postgresql":
            # An index build may rightly take longer than an API query is allowed to.
            await conn.exec_driver_sql("SET LOCAL statement_timeout = 0")
            await conn.exec_driver_sql("SET LOCAL lock_timeout = '60s'")
        await conn.run_sync(lambda sync: command.upgrade(_config(sync), revision))


async def current(engine: AsyncEngine) -> str | None:
    async with engine.connect() as conn:
        return await conn.run_sync(
            lambda sync: MigrationContext.configure(sync).get_current_revision()
        )


async def require_head(engine: AsyncEngine) -> None:
    found, expected = await current(engine), head()
    if found != expected:
        raise SchemaOutOfDateError(
            f"database schema is at {found or 'no revision'}, this version needs {expected}: "
            "run `agency db upgrade` first"
        )


async def autogenerate(engine: AsyncEngine, message: str) -> None:
    """Write a new revision from the difference between the models and the database."""
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda sync: command.revision(_config(sync), message=message, autogenerate=True)
        )
