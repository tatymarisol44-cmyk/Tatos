from __future__ import annotations

from pathlib import Path

import pytest

from orchestrator.catalog import Catalog, load_catalog
from orchestrator.config import Settings
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
    )


@pytest.fixture
def catalog() -> Catalog:
    return load_catalog(FIXTURES)


@pytest.fixture
def fake_llm() -> FakeLLM:
    return FakeLLM()


@pytest.fixture
async def orchestrator(settings: Settings, catalog: Catalog, fake_llm: FakeLLM) -> Orchestrator:
    orch = Orchestrator(settings, catalog=catalog, llm=fake_llm)
    await orch.start()
    return orch
