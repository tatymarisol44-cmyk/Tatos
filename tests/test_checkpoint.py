from __future__ import annotations

import os
import uuid

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import SecretStr

from orchestrator.catalog import Catalog
from orchestrator.checkpoint import Checkpointer
from orchestrator.config import Settings
from orchestrator.llm import FakeLLM
from orchestrator.service import Orchestrator

POSTGRES_URL = os.environ.get("TEST_POSTGRES_URL")


async def test_memory_is_the_default(settings: Settings) -> None:
    cp = Checkpointer(settings)
    assert isinstance(cp.saver, InMemorySaver)
    await cp.start()
    await cp.close()


def test_postgres_requires_url(settings: Settings) -> None:
    settings.checkpointer_backend = "postgres"
    with pytest.raises(ValueError, match="POSTGRES_URL"):
        Checkpointer(settings)


async def test_postgres_saver_is_built_without_connecting(settings: Settings) -> None:
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    settings.checkpointer_backend = "postgres"
    settings.postgres_url = SecretStr("postgresql://nobody@unreachable.invalid/db")
    cp = Checkpointer(settings)
    assert isinstance(cp.saver, AsyncPostgresSaver)
    assert cp._pool.closed  # opened only by start()


async def test_memory_threads_do_not_survive_a_new_instance(
    settings: Settings, catalog: Catalog
) -> None:
    """The limitation Postgres fixes: a restart (or another replica) forgets the thread."""
    first = Orchestrator(settings, catalog=catalog, llm=FakeLLM())
    await first.start()
    await first.chat("rank higher on google", thread_id="t", tenant="acme")

    llm = FakeLLM()
    second = Orchestrator(settings, catalog=catalog, llm=llm)
    await second.start()
    await second.chat("and keywords?", thread_id="t", tenant="acme")
    assert [m["role"] for m in llm.calls[-1]] == ["system", "user"]


@pytest.mark.skipif(POSTGRES_URL is None, reason="set TEST_POSTGRES_URL to run")
async def test_postgres_thread_survives_a_new_instance(
    settings: Settings, catalog: Catalog
) -> None:
    settings.checkpointer_backend = "postgres"
    settings.postgres_url = SecretStr(POSTGRES_URL or "")
    thread = f"t-{uuid.uuid4().hex}"

    first = Orchestrator(settings, catalog=catalog, llm=FakeLLM())
    await first.start()
    try:
        await first.chat("rank higher on google", thread_id=thread, tenant="acme")
    finally:
        await first.close()

    # A fresh process/replica pointing at the same database picks up the history.
    llm = FakeLLM()
    second = Orchestrator(settings, catalog=catalog, llm=llm)
    await second.start()
    try:
        await second.chat("and keywords?", thread_id=thread, tenant="acme")
    finally:
        await second.close()
    messages = llm.calls[-1]
    assert [m["role"] for m in messages] == ["system", "user", "assistant", "user"]
    assert messages[1]["content"] == "rank higher on google"
