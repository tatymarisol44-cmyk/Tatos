"""Composition root shared by the HTTP API, the A2A endpoint, the MCP server and the CLI."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Literal

from langchain_core.runnables import RunnableConfig

from orchestrator.catalog import Catalog, load_catalog
from orchestrator.config import Settings
from orchestrator.embeddings import Embedder, HashingEmbedder, LiteLLMEmbedder
from orchestrator.graph import build_graph
from orchestrator.knowledge import (
    ChunkStore,
    InMemoryChunkStore,
    KnowledgeBase,
    QdrantChunkStore,
)
from orchestrator.llm import LLMClient, build_llm
from orchestrator.router import Router, RoutingDecision
from orchestrator.vectorstore import InMemoryVectorStore, QdrantVectorStore, VectorStore

Mode = Literal["single", "team"]


@dataclass
class ChatResult:
    thread_id: str
    blocked: bool
    answer: str | None
    routing: dict[str, Any] | None
    guardrails: dict[str, list[str]]
    usage: dict[str, Any] = field(default_factory=dict)
    mode: Mode = "single"
    team: dict[str, Any] | None = None
    sources: list[dict[str, Any]] = field(default_factory=list)


def build_embedder(settings: Settings) -> Embedder:
    if settings.embedding_backend == "litellm":
        return LiteLLMEmbedder(settings.embedding_model)
    return HashingEmbedder()


def build_store(settings: Settings) -> VectorStore:
    if settings.vector_backend == "qdrant":
        key = settings.qdrant_api_key.get_secret_value() if settings.qdrant_api_key else None
        return QdrantVectorStore(settings.qdrant_url, key)
    return InMemoryVectorStore()


def build_chunk_store(settings: Settings) -> ChunkStore:
    if settings.vector_backend == "qdrant":
        key = settings.qdrant_api_key.get_secret_value() if settings.qdrant_api_key else None
        return QdrantChunkStore(settings.qdrant_url, key)
    return InMemoryChunkStore()


def _sources(chunks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Citation list matching the [n] markers the agents were asked to use."""
    return [
        {
            "n": i,
            "doc_id": c["doc_id"],
            "title": c["title"],
            "score": round(c["score"], 4),
            "excerpt": c["text"][:300],
        }
        for i, c in enumerate(chunks, 1)
    ]


def _total(parts: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "input_tokens": sum(p.get("input_tokens", 0) for p in parts),
        "output_tokens": sum(p.get("output_tokens", 0) for p in parts),
        "cost_usd": round(sum(p.get("cost_usd", 0.0) for p in parts), 6),
        "llm_calls": len(parts),
    }


def _usage(state: dict[str, Any]) -> dict[str, Any]:
    """Per-stage usage plus a `total`, so a SaaS can meter and bill each request."""
    usage: dict[str, Any] = {}
    decision, plan = state.get("decision"), state.get("plan")
    if decision and decision.get("usage"):
        usage["router"] = decision["usage"]
    if plan:
        if plan.get("usage"):
            usage["planner"] = plan["usage"]
        agents = [r["usage"] for r in state.get("results", []) if r.get("usage")]
        if agents:
            usage["agents"] = agents
        if state.get("answer_usage"):
            usage["synthesizer"] = state["answer_usage"]
    elif state.get("answer_usage"):
        usage["agent"] = state["answer_usage"]
    parts = [p for v in usage.values() for p in (v if isinstance(v, list) else [v])]
    usage["total"] = _total(parts)
    return usage


class Orchestrator:
    def __init__(
        self, settings: Settings, catalog: Catalog | None = None, llm: LLMClient | None = None
    ) -> None:
        self.settings = settings
        self.catalog = catalog or load_catalog(settings.agents_dir)
        self.llm = llm or build_llm(settings)
        embedder = build_embedder(settings)
        self.router = Router(self.catalog, embedder, build_store(settings), self.llm, settings)
        self.knowledge = KnowledgeBase(embedder, build_chunk_store(settings), settings)
        self.graph = build_graph(self.catalog, self.router, self.llm, settings, self.knowledge)
        self.ready = False

    async def start(self) -> None:
        await self.router.build_index()
        await self.knowledge.start()
        self.ready = True

    async def route(self, question: str) -> RoutingDecision:
        return await self.router.route(question)

    def _prepare(
        self,
        question: str,
        thread_id: str | None,
        agent_id: str | None,
        agent_ids: list[str] | None,
        mode: Mode,
        tenant: str,
    ) -> tuple[str, dict[str, Any], RunnableConfig]:
        unknown = [a for a in [agent_id, *(agent_ids or [])] if a and a not in self.catalog.agents]
        if unknown:
            raise KeyError(f"Unknown agent_id: {', '.join(unknown)}")
        thread_id = thread_id or str(uuid.uuid4())
        # Threads are namespaced by tenant so one tenant can never read another's history.
        config: RunnableConfig = {
            "configurable": {"thread_id": f"{tenant}:{thread_id}"},
            "max_concurrency": self.settings.team_max_concurrency,
        }
        inputs = {
            "question": question,
            "tenant": tenant,
            "mode": mode,
            "agent_override": agent_id,
            "agent_ids": agent_ids or None,
        }
        return thread_id, inputs, config

    @staticmethod
    def _result(thread_id: str, mode: Mode, state: dict[str, Any]) -> ChatResult:
        plan = state.get("plan")
        return ChatResult(
            thread_id=thread_id,
            blocked=bool(state.get("blocked")),
            answer=state.get("answer"),
            routing=state.get("decision"),
            guardrails={
                "reasons": state.get("guardrail_reasons", []),
                "flags": state.get("guardrail_flags", []),
            },
            usage=_usage(state),
            mode=mode,
            team={"plan": plan, "results": state.get("results", [])} if plan else None,
            sources=_sources(state.get("knowledge", [])),
        )

    async def chat(
        self,
        question: str,
        *,
        thread_id: str | None = None,
        agent_id: str | None = None,
        agent_ids: list[str] | None = None,
        mode: Mode = "single",
        tenant: str = "default",
    ) -> ChatResult:
        thread_id, inputs, config = self._prepare(
            question, thread_id, agent_id, agent_ids, mode, tenant
        )
        state = await self.graph.ainvoke(inputs, config)
        return self._result(thread_id, mode, state)

    async def chat_stream(
        self,
        question: str,
        *,
        thread_id: str | None = None,
        agent_id: str | None = None,
        agent_ids: list[str] | None = None,
        mode: Mode = "single",
        tenant: str = "default",
    ) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        """Yields `(event, data)` as each graph node finishes: `start`, `guardrails`,
        `knowledge` (if the tenant's documents matched), `routing` or `plan`, one `step`
        per specialist, then `done` with the full result.
        Validation errors raise before the first event, so callers can still return 4xx."""
        thread_id, inputs, config = self._prepare(
            question, thread_id, agent_id, agent_ids, mode, tenant
        )

        async def events() -> AsyncIterator[tuple[str, dict[str, Any]]]:
            yield "start", {"thread_id": thread_id, "mode": mode}
            async for chunk in self.graph.astream(inputs, config, stream_mode="updates"):
                for node, update in chunk.items():
                    if not update:
                        continue
                    if node == "input_guard":
                        yield (
                            "guardrails",
                            {
                                "blocked": update["blocked"],
                                "reasons": update["guardrail_reasons"],
                                "flags": update["guardrail_flags"],
                            },
                        )
                    elif node == "knowledge" and update.get("knowledge"):
                        yield "knowledge", {"sources": _sources(update["knowledge"])}
                    elif node == "route":
                        yield "routing", update["decision"]
                    elif node == "plan":
                        yield "plan", update["plan"]
                    elif node == "worker":
                        for result in update["results"]:
                            yield "step", result
            snapshot = await self.graph.aget_state(config)
            yield "done", self._result(thread_id, mode, dict(snapshot.values)).__dict__

        return events()
