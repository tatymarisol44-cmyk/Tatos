"""Adapters: Qdrant store, LiteLLM clients, CLI and MCP tools (no network)."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from orchestrator import cli, mcp_server
from orchestrator.config import Settings
from orchestrator.embeddings import HashingEmbedder, LiteLLMEmbedder
from orchestrator.llm import LiteLLMClient, build_llm
from orchestrator.service import build_embedder, build_store
from orchestrator.vectorstore import InMemoryVectorStore, QdrantVectorStore
from tests.conftest import FIXTURES


async def test_qdrant_store_roundtrip() -> None:
    emb = HashingEmbedder(dim=128)
    store = QdrantVectorStore(":memory:")
    assert await store.ensure("agents_v1", emb.dim) is False
    docs = {"seo": "google search keywords", "k8s": "kubernetes docker deploy"}
    await store.upsert(list(docs), await emb.embed(list(docs.values())))
    assert await store.ensure("agents_v1", emb.dim) is True
    [query] = await emb.embed(["kubernetes deploy"])
    hits = await store.search(query, k=1)
    assert hits[0].agent_id == "k8s"


def test_factories(settings: Settings) -> None:
    assert isinstance(build_store(settings), InMemoryVectorStore)
    assert isinstance(build_embedder(settings), HashingEmbedder)
    settings.embedding_backend = "litellm"
    settings.llm_backend = "litellm"
    assert isinstance(build_embedder(settings), LiteLLMEmbedder)
    assert isinstance(build_llm(settings), LiteLLMClient)


async def test_litellm_client_maps_response(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    import litellm

    seen: dict[str, Any] = {}

    async def fake_acompletion(**kwargs: Any) -> Any:
        seen.update(kwargs)
        return SimpleNamespace(
            model="claude-x",
            choices=[SimpleNamespace(message=SimpleNamespace(content="hola"))],
            usage=SimpleNamespace(prompt_tokens=11, completion_tokens=3),
        )

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    monkeypatch.setattr(litellm, "completion_cost", lambda **_: 0.0012)
    result = await LiteLLMClient(settings).complete(
        [{"role": "user", "content": "hi"}], model="anthropic/claude-x"
    )
    assert result.usage() == {
        "model": "claude-x",
        "input_tokens": 11,
        "output_tokens": 3,
        "cost_usd": 0.0012,
    }
    assert seen["fallbacks"] == settings.llm_fallback_models
    assert seen["num_retries"] == settings.llm_num_retries


async def test_litellm_embedder_batches(monkeypatch: pytest.MonkeyPatch) -> None:
    import litellm

    batches: list[int] = []

    async def fake_aembedding(model: str, input: list[str]) -> Any:
        batches.append(len(input))
        return SimpleNamespace(data=[{"embedding": [float(len(t))]} for t in input])

    monkeypatch.setattr(litellm, "aembedding", fake_aembedding)
    emb = LiteLLMEmbedder("openai/text-embedding-3-small", batch_size=2)
    assert await emb.embed(["a", "bb", "ccc"]) == [[1.0], [2.0], [3.0]]
    assert batches == [2, 1]
    assert emb.signature == "openai-text-embedding-3-small"


@pytest.fixture
def offline_env(monkeypatch: pytest.MonkeyPatch) -> None:
    from orchestrator.config import get_settings

    monkeypatch.setenv("AGENTS_DIR", str(FIXTURES))
    monkeypatch.setenv("LLM_BACKEND", "fake")
    monkeypatch.setenv("ROUTER_USE_LLM", "false")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_cli_route_and_eval(
    offline_env: None, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["route", "kubernetes docker deploy"]) == 0
    assert json.loads(capsys.readouterr().out)["agent_id"] == "engineering-devops-automator"

    dataset = tmp_path / "ds.jsonl"
    dataset.write_text(
        json.dumps({"question": "google seo", "expected": ["marketing-seo-specialist"]}) + "\n",
        encoding="utf-8",
    )
    out = tmp_path / "report.json"
    assert (
        cli.main(["eval", "--dataset", str(dataset), "--min-top1", "1", "--output", str(out)]) == 0
    )
    assert json.loads(out.read_text(encoding="utf-8"))["top1_accuracy"] == 1.0

    dataset.write_text(
        json.dumps({"question": "google seo", "expected": ["security-penetration-tester"]}) + "\n",
        encoding="utf-8",
    )
    assert cli.main(["eval", "--dataset", str(dataset), "--min-top1", "1"]) == 1


def test_cli_index_and_ask(offline_env: None, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["index"]) == 0
    assert json.loads(capsys.readouterr().out)["agents"] == 4
    assert cli.main(["ask", "hello", "--agent-id", "marketing-seo-specialist"]) == 0
    assert json.loads(capsys.readouterr().out)["answer"].startswith("[# SEO Specialist]")
    assert cli.main(["ask", "seo audit", "--team", "marketing-seo-specialist"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["mode"] == "team"
    assert [s["agent_id"] for s in out["team"]["plan"]["steps"]] == ["marketing-seo-specialist"]


async def test_mcp_tools(offline_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mcp_server, "_orch", None)
    agents = await mcp_server.list_agents("security")
    assert [a["id"] for a in agents] == ["security-penetration-tester"]
    decision = await mcp_server.route_question("kubernetes docker")
    assert decision["agent_id"] == "engineering-devops-automator"
    answer = await mcp_server.ask("hi", agent_id="marketing-seo-specialist")
    assert answer["answer"].startswith("[# SEO Specialist]")
    team = await mcp_server.ask_team("kubernetes docker and google seo")
    assert team["mode"] == "team" and len(team["team"]["results"]) == 2
    tools = {t.name for t in await mcp_server.mcp.list_tools()}
    assert tools == {"list_agents", "route_question", "ask", "ask_team"}
