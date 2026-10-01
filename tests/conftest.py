from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from orchestrator.catalog import Catalog, load_catalog
from orchestrator.config import Settings
from orchestrator.db import Database
from orchestrator.llm import FakeLLM
from orchestrator.service import Orchestrator

FIXTURES = Path(__file__).parent / "fixtures" / "agents"


@pytest.fixture
def settings() -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        app_env="test",
        agents_dir=FIXTURES,
        llm_backend="fake",
        embedding_backend="hashing",
        vector_backend="memory",
        router_top_k=3,
        api_keys="test-key:acme,other-key:globex",  # type: ignore[arg-type]
        rate_limit_per_minute=1000,
        outbox_interval_seconds=0,  # tests drive the outbox explicitly
    )


@pytest.fixture(autouse=True)
async def _dispose_databases(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[None]:
    """Many tests build an Orchestrator without closing it; dispose every database engine
    they opened, or aiosqlite's worker thread outlives the test's event loop."""
    opened: list[Database] = []
    original = Database.__init__

    def tracking_init(self: Database, settings: Settings) -> None:
        original(self, settings)
        opened.append(self)

    monkeypatch.setattr(Database, "__init__", tracking_init)
    yield
    for db in opened:
        await db.close()


@pytest.fixture
def catalog() -> Catalog:
    return load_catalog(FIXTURES)


@pytest.fixture
def fake_llm() -> FakeLLM:
    return FakeLLM()


@pytest.fixture
async def orchestrator(
    settings: Settings, catalog: Catalog, fake_llm: FakeLLM
) -> AsyncIterator[Orchestrator]:
    orch = Orchestrator(settings, catalog=catalog, llm=fake_llm)
    await orch.start()
    yield orch
    await orch.close()
