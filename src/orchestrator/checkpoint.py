"""Conversation-state persistence (LangGraph checkpointer).

- memory:   per-process RAM. Fine for dev/tests; lost on restart, not shared by replicas.
- postgres: durable and shared, so any replica can continue any thread (horizontal scale).

The Postgres saver is built unopened: `Checkpointer.start()` opens the pool and creates
the tables (idempotent), `close()` releases the connections on shutdown.

Retention and erasure (GDPR) go through the saver's own API (`alist`/`adelete_thread`), so
they behave the same on every backend. Thread keys are "{tenant}:{thread_id}"."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver

from orchestrator.config import Settings

_SECURE_SSLMODES = {"require", "verify-ca", "verify-full"}


class Checkpointer:
    def __init__(self, settings: Settings) -> None:
        self._pool: Any = None
        self.saver: BaseCheckpointSaver[Any]
        if settings.checkpointer_backend == "postgres":
            if settings.postgres_url is None:
                raise ValueError("CHECKPOINTER_BACKEND=postgres requires POSTGRES_URL")
            from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
            from psycopg.conninfo import conninfo_to_dict
            from psycopg.rows import dict_row
            from psycopg_pool import AsyncConnectionPool

            # Conversations are customer data: in prod they must travel encrypted.
            sslmode = conninfo_to_dict(settings.postgres_url.get_secret_value()).get("sslmode")
            if (
                settings.app_env == "prod"
                and not settings.postgres_allow_insecure
                and sslmode not in _SECURE_SSLMODES
            ):
                raise ValueError(
                    "POSTGRES_URL needs sslmode=require|verify-ca|verify-full in prod "
                    "(or POSTGRES_ALLOW_INSECURE=true for a private, encrypted network)"
                )

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

    async def threads(self) -> AsyncIterator[tuple[str, datetime | None]]:
        """Every thread key with the time of its latest checkpoint (newest first per
        thread, as the savers list them)."""
        seen: set[str] = set()
        async for item in self.saver.alist(None):
            key = str(item.config["configurable"]["thread_id"])
            if key in seen:
                continue
            seen.add(key)
            ts = item.checkpoint.get("ts")
            yield key, datetime.fromisoformat(ts) if isinstance(ts, str) else None

    async def exists(self, key: str) -> bool:
        return await self.saver.aget_tuple({"configurable": {"thread_id": key}}) is not None

    async def delete(self, key: str) -> None:
        await self.saver.adelete_thread(key)

    async def delete_prefix(self, prefix: str) -> int:
        keys = [k async for k, _ in self.threads() if k.startswith(prefix)]
        for key in keys:
            await self.delete(key)
        return len(keys)

    async def purge(self, older_than: timedelta) -> int:
        """Retention: delete threads whose latest activity is older than `older_than`."""
        cutoff = datetime.now(UTC) - older_than
        keys = [k async for k, ts in self.threads() if ts is not None and ts < cutoff]
        for key in keys:
            await self.delete(key)
        return len(keys)
