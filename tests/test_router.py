from __future__ import annotations

import json

import pytest

from orchestrator.catalog import Catalog
from orchestrator.config import Settings
from orchestrator.embeddings import HashingEmbedder
from orchestrator.llm import FakeLLM, LLMResult, Message
from orchestrator.router import Router
from orchestrator.vectorstore import InMemoryVectorStore


def make_router(catalog: Catalog, settings: Settings, llm: object) -> Router:
    return Router(catalog, HashingEmbedder(), InMemoryVectorStore(), llm, settings)  # type: ignore[arg-type]


async def test_index_is_built_once_per_catalog_version(
    catalog: Catalog, settings: Settings
) -> None:
    router = make_router(catalog, settings, FakeLLM())
    assert await router.build_index() is True
    assert await router.build_index() is False
    assert catalog.version in router.index_name


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("my react bundle is slow", "engineering-frontend-developer"),
        ("deploy with docker and kubernetes", "engineering-devops-automator"),
        ("rank higher on google search", "marketing-seo-specialist"),
        ("penetration test of our web application", "security-penetration-tester"),
    ],
)
async def test_retrieval_routing(
    catalog: Catalog, settings: Settings, question: str, expected: str
) -> None:
    settings.router_use_llm = False
    router = make_router(catalog, settings, FakeLLM())
    await router.build_index()
    decision = await router.route(question)
    assert decision.agent_id == expected
    assert decision.method == "retrieval"


async def test_llm_picks_among_candidates(catalog: Catalog, settings: Settings) -> None:
    llm = FakeLLM(
        router_reply=json.dumps(
            {"agent_id": "marketing-seo-specialist", "confidence": 0.8, "reasoning": "seo"}
        )
    )
    router = make_router(catalog, settings, llm)
    await router.build_index()
    decision = await router.route("rank our react site higher on google")
    assert decision.method == "llm"
    assert decision.agent_id == "marketing-seo-specialist"
    assert decision.usage is not None
    assert "ROUTER" in llm.calls[0][0]["content"]


@pytest.mark.parametrize(
    "reply",
    [
        '{"agent_id": "made-up-agent", "confidence": 0.99}',
        "not json at all",
        '{"agent_id": "marketing-seo-specialist", "confidence": 0.1}',
        '{"agent_id": "marketing-seo-specialist", "confidence": "high"}',
    ],
)
async def test_invalid_llm_output_falls_back_to_retrieval(
    catalog: Catalog, settings: Settings, reply: str
) -> None:
    router = make_router(catalog, settings, FakeLLM(router_reply=reply))
    await router.build_index()
    decision = await router.route("deploy with docker and kubernetes")
    assert decision.method == "retrieval"
    assert decision.agent_id == "engineering-devops-automator"


async def test_llm_exception_falls_back(catalog: Catalog, settings: Settings) -> None:
    class Boom:
        async def complete(self, messages: list[Message], **_: object) -> LLMResult:
            raise TimeoutError("provider down")

    router = make_router(catalog, settings, Boom())
    await router.build_index()
    decision = await router.route("deploy with docker and kubernetes")
    assert decision.method == "retrieval"


async def test_override_and_unknown_override(catalog: Catalog, settings: Settings) -> None:
    router = make_router(catalog, settings, FakeLLM())
    await router.build_index()
    decision = await router.route("anything", override="security-penetration-tester")
    assert decision.method == "override"
    with pytest.raises(KeyError):
        await router.route("anything", override="nope")


async def test_no_match_uses_default_agent(catalog: Catalog, settings: Settings) -> None:
    settings.router_use_llm = False
    settings.default_agent_id = "engineering-devops-automator"
    router = make_router(catalog, settings, FakeLLM())
    await router.build_index()
    decision = await router.route("zzzz qqqq")
    assert decision.method == "default"
    assert decision.agent_id == "engineering-devops-automator"
