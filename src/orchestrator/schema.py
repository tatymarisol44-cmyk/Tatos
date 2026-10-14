"""Every table of the platform, registered in one place, and the database prepared from it.

Importing a feature module registers its tables on `db.metadata`. The running API, the
maintenance jobs and the migrations (Alembic's env.py) all load this module, so they see
the same tables. Before it, two hand-kept lists disagreed: the migrations missed 8 of 34
tables (clinical files, test results, privacy cases...), and Alembic's autogenerate would
have proposed dropping them.

This is the "main component" of Clean Architecture (ch. 26): it knows every feature, so
`db` (infrastructure) knows none and no longer closes an import cycle through them.
tests/test_schema.py fails when a module defines a table and is not imported here."""

from __future__ import annotations

from orchestrator import (  # noqa: F401  (imported for the tables they register)
    agenda,
    auth,
    campaigns,
    clinical_files,
    clinical_records,
    crm,
    establishment,
    governance,
    inbound,
    inbox,
    instruments,
    knowledge,
    migrate,
    oncall,
    privacy_cases,
    publishing,
    social,
    spend,
    whatsapp_assistant,
)
from orchestrator.db import Database, metadata

__all__ = ["metadata", "owned_tables", "prepare"]

# Tables the platform owns but does not declare: the migration version (Alembic) and the
# conversation checkpoints (LangGraph's Postgres saver creates and migrates its own).
UNDECLARED = (
    "alembic_version",
    "checkpoints",
    "checkpoint_blobs",
    "checkpoint_writes",
    "checkpoint_migrations",
)


def owned_tables() -> frozenset[str]:
    """Every table the platform owns. A database may hold others (a provider's sample
    tables, another project's): the backup leaves those out and names them."""
    return frozenset(metadata.tables) | frozenset(UNDECLARED)


async def prepare(db: Database) -> None:
    """SQLite (dev, tests) gets its tables created; Postgres is migrated when the
    deployment allows it (DB_AUTO_MIGRATE), otherwise only checked to be at head."""
    if db.engine.dialect.name == "sqlite":
        async with db.engine.begin() as conn:
            await conn.run_sync(metadata.create_all)
    elif db.settings.db_auto_migrate:
        await migrate.upgrade(db.engine)
    else:
        await migrate.require_head(db.engine)
