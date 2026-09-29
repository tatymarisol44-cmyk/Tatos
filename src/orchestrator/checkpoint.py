"""Conversation-state persistence (LangGraph checkpointer).

- memory:   per-process RAM. Fine for dev/tests; lost on restart, not shared by replicas.
- postgres: durable and shared, so any replica can continue any thread (horizontal scale).

The Postgres saver is built unopened: `Checkpointer.start()` opens the pool and creates
the tables (idempotent), `close()` releases the connections on shutdown."""

from __future__ import annotations

from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver

from orchestrator.config import Settings


class Checkpointer:
    def __init__(self, settings: Settings) -> None:
        self._pool: Any = None
        self.saver: BaseCheckpointSaver[Any]
        if settings.checkpointer_backend == "postgres":
            if settings.postgres_url is None:
                raise ValueError("CHECKPOINTER_BACKEND=postgres requires POSTGRES_URL")
            from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
            from psycopg.rows import dict_row
            from psycopg_pool import AsyncConnectionPool

            self._pool = AsyncConnectionPool(
                settings.postgres_url.get_secret_value(),
                min_size=1,
                max_size=settings.postgres_pool_size,
                open=False,
                # Required by the saver: it manages transactions itself and returns dict rows.
                # prepare_threshold=0 keeps it working behind PgBouncer (transaction mode).
                kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
            )
            self.saver = AsyncPostgresSaver(self._pool)
        else:
            self.saver = InMemorySaver()

    async def start(self) -> None:
        if self._pool is not None:
            await self._pool.open(wait=True)
            await self.saver.setup()  # type: ignore[attr-defined]

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
