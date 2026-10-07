"""Alembic environment. Run through `orchestrator.migrate` (or `agency db ...`), which hands
over an open connection; there is no alembic.ini and no URL here."""

from __future__ import annotations

from typing import Any

from alembic import context

# Advisory lock id: replicas that start together migrate one at a time (Postgres).
MIGRATION_LOCK = 727_274


def _metadata() -> Any:
    # Each module registers its tables on the shared MetaData when imported.
    from orchestrator import (  # noqa: F401
        auth,
        campaigns,
        crm,
        governance,
        knowledge,
        publishing,
        social,
    )
    from orchestrator.db import metadata

    return metadata


def run_migrations_online() -> None:
    connection = context.config.attributes.get("connection")
    if connection is None:
        raise RuntimeError("run migrations with `agency db upgrade` (orchestrator.migrate)")
    context.configure(
        connection=connection,
        target_metadata=_metadata(),
        compare_type=True,
        render_as_batch=connection.dialect.name == "sqlite",  # ALTER TABLE on SQLite
    )
    with context.begin_transaction():
        if connection.dialect.name == "postgresql":
            connection.exec_driver_sql(f"SELECT pg_advisory_xact_lock({MIGRATION_LOCK})")
        context.run_migrations()


if context.is_offline_mode():
    raise RuntimeError("offline (SQL script) migrations are not supported")
run_migrations_online()
