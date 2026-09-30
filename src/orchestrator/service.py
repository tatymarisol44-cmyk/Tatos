"""Composition root shared by the HTTP API, the A2A endpoint, the MCP server and the CLI."""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from langchain_core.runnables import RunnableConfig
from langgraph.types import Command

from orchestrator import packs
from orchestrator.campaigns import CampaignService
from orchestrator.catalog import Catalog, load_catalog
from orchestrator.checkpoint import Checkpointer
from orchestrator.config import Settings
from orchestrator.crm import CrmService
from orchestrator.db import Database
from orchestrator.embeddings import Embedder, HashingEmbedder, LiteLLMEmbedder
from orchestrator.governance import AuditLog, ConsentRegistry, ReviewQueue
from orchestrator.graph import build_graph
from orchestrator.insights import InsightsService
from orchestrator.knowledge import (
    ChunkStore,
    InMemoryChunkStore,
    KnowledgeBase,
    QdrantChunkStore,
)
from orchestrator.llm import LLMClient, build_llm
from orchestrator.memory import InMemoryMemoryStore, MemoryStore, QdrantMemoryStore, SemanticMemory
from orchestrator.remote import A2AClient, discover_all
from orchestrator.router import Router, RoutingDecision
from orchestrator.vectorstore import InMemoryVectorStore, QdrantVectorStore, VectorStore

log = logging.getLogger(__name__)

Mode = Literal["single", "team"]
Status = Literal["completed", "blocked", "pending_review", "rejected"]


class PendingReviewError(RuntimeError):
    """The thread is paused waiting for a human decision; it cannot take a new message."""


class ReviewNotFoundError(KeyError):
    """No pending review for this thread (never opened, already resolved, other tenant)."""


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
    status: Status = "completed"
    # While pending: what the reviewer sees (draft, risk reasons, sources).
    review: dict[str, Any] | None = None
    evidence: dict[str, Any] | None = None
    citations: dict[str, Any] | None = None
    route_log: list[dict[str, Any]] = field(default_factory=list)
    decision_record: dict[str, Any] | None = None
    memory: dict[str, int] = field(default_factory=dict)


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


def build_memory_store(settings: Settings) -> MemoryStore:
    if settings.vector_backend == "qdrant":
        key = settings.qdrant_api_key.get_secret_value() if settings.qdrant_api_key else None
        return QdrantMemoryStore(settings.qdrant_url, key)
    return InMemoryMemoryStore()


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
        self,
        settings: Settings,
        catalog: Catalog | None = None,
        llm: LLMClient | None = None,
        remote: A2AClient | None = None,
    ) -> None:
        self.settings = settings
        self.catalog = catalog or load_catalog(settings.agents_dir)
        self.llm = llm or build_llm(settings)
        embedder = build_embedder(settings)
        self.router = Router(self.catalog, embedder, build_store(settings), self.llm, settings)
        self.knowledge = KnowledgeBase(embedder, build_chunk_store(settings), settings)
        self.memory = SemanticMemory(embedder, build_memory_store(settings), self.llm, settings)
        self.checkpointer = Checkpointer(settings)
        self.db = Database(settings)
        self.audit = AuditLog(self.db)
        self.reviews = ReviewQueue(self.db)
        self.consents = ConsentRegistry(self.db, self.audit)
        self.crm = CrmService(self.db, self.audit, settings)
        self.insights = InsightsService(self.db, self.crm, self.llm, settings)
        self.campaigns = CampaignService(
            self.db, self.audit, self.consents, self.crm, self.insights, self.llm, settings
        )
        self.remote = remote or (A2AClient(settings) if settings.remote_agents else None)
        self.graph = build_graph(
            self.catalog,
            self.router,
            self.llm,
            settings,
            self.knowledge,
            self.checkpointer.saver,
            self.remote,
            self.memory,
            self.consents,
        )
        self.ready = False

    async def start(self) -> None:
        packs.validate_config(self.settings)
        await self.db.start()
        await self.checkpointer.start()
        if self.remote is not None:
            # Before indexing: remote agents are routed like local ones.
            specs = await discover_all(self.remote, self.settings.remote_agents)
            added = {s.id for s in self.catalog.add_remote(specs)}
            for spec in specs:
                if spec.id not in added:  # same id as a local agent: it would be unroutable
                    log.error(
                        "remote agent %s NOT registered: id %s is taken by a local agent",
                        spec.path,
                        spec.id,
                    )
        await self.router.build_index()
        await self.knowledge.start()
        await self.memory.start()
        self.ready = True

    async def close(self) -> None:
        self.ready = False
        if self.remote is not None:
            await self.remote.close()
        await self.campaigns.close()
        await self.checkpointer.close()
        await self.db.close()

    @staticmethod
    def thread_key(tenant: str, thread_id: str) -> str:
        return f"{tenant}:{thread_id}"

    async def delete_thread(self, tenant: str, thread_id: str) -> bool:
        """Erase one conversation of this tenant. False if it did not exist."""
        key = self.thread_key(tenant, thread_id)
        if not await self.checkpointer.exists(key):
            return False
        await self.checkpointer.delete(key)
        await self.reviews.delete_thread(tenant, thread_id)
        return True

    async def delete_tenant_threads(self, tenant: str) -> int:
        """Erase every conversation of a tenant (right to erasure / offboarding)."""
        prefix = self.thread_key(tenant, "")
        keys = [k async for k, _ in self.checkpointer.threads() if k.startswith(prefix)]
        for key in keys:
            await self.checkpointer.delete(key)
            await self.reviews.delete_thread(tenant, key.removeprefix(prefix))
        return len(keys)

    async def purge_threads(self, older_than: timedelta) -> int:
        """Retention: delete threads whose latest activity is older than `older_than`."""
        cutoff = datetime.now(UTC) - older_than
        keys = [k async for k, ts in self.checkpointer.threads() if ts is not None and ts < cutoff]
        for key in keys:
            await self.checkpointer.delete(key)
        return len(keys)

    async def purge_memory(self) -> int:
        """Retention: delete memory facts past their expiry date."""
        return await self.memory.purge_expired()

    # --- data-subject rights (GDPR Art. 15/17/20, HIPAA access, LOPDP) ------------
    async def export_subject(self, tenant: str, subject_id: str, actor: str) -> dict[str, Any]:
        """Everything held about one subject, in a portable structure."""
        data = {
            "subject_id": subject_id,
            "consents": await self.consents.get(tenant, subject_id),
            "memory": [f.to_dict() for f in await self.memory.export(tenant, subject_id)],
            "crm": await self.crm.export_subject(tenant, subject_id),
            "campaign_messages": await self.campaigns.export_subject(tenant, subject_id),
            "audit": [e.to_dict() for e in await self.audit.list(tenant, subject_id=subject_id)],
        }
        await self.audit.record(tenant, actor, "subject.exported", "subject", subject_id=subject_id)
        return data

    async def erase_subject(self, tenant: str, subject_id: str, actor: str) -> dict[str, Any]:
        """Right to erasure. Memory, consents, marketing history and contact data go; the
        clinical record is kept (restricted) where the law requires it (GDPR Art. 17(3),
        HIPAA/State retention rules) and the audit trail stays to prove the erasure."""
        result = {
            "memory_facts": await self.memory.erase(tenant, subject_id),
            "consents": await self.consents.erase(tenant, subject_id),
            "campaign_messages": await self.campaigns.erase_subject(tenant, subject_id),
            "crm": await self.crm.erase_subject(tenant, subject_id),
        }
        await self.audit.record(
            tenant, actor, "subject.erased", "subject", subject_id=subject_id, details=result
        )
        return result

    async def route(self, question: str) -> RoutingDecision:
        return await self.router.route(question)

    def _config(self, tenant: str, thread_id: str) -> RunnableConfig:
        # Threads are namespaced by tenant so one tenant can never read another's history.
        return {
            "configurable": {"thread_id": self.thread_key(tenant, thread_id)},
            "max_concurrency": self.settings.team_max_concurrency,
        }

    async def _prepare(
        self,
        question: str,
        thread_id: str | None,
        agent_id: str | None,
        agent_ids: list[str] | None,
        mode: Mode,
        tenant: str,
        subject_id: str | None,
        force_review: bool,
    ) -> tuple[str, dict[str, Any], RunnableConfig]:
        unknown = [a for a in [agent_id, *(agent_ids or [])] if a and a not in self.catalog.agents]
        if unknown:
            raise KeyError(f"Unknown agent_id: {', '.join(unknown)}")
        thread_id = thread_id or str(uuid.uuid4())
        pending = await self.reviews.get(tenant, thread_id)
        if pending is not None and pending.status == "pending":
            # A new input would start a fresh run and orphan the paused one.
            raise PendingReviewError(f"thread {thread_id} is waiting for a human review")
        inputs = {
            "question": question,
            "tenant": tenant,
            "mode": mode,
            "agent_override": agent_id,
            "agent_ids": agent_ids or None,
            "subject_id": subject_id,
            "force_review": force_review,
        }
        return thread_id, inputs, self._config(tenant, thread_id)

    @staticmethod
    def _result(thread_id: str, mode: Mode, state: dict[str, Any]) -> ChatResult:
        plan = state.get("plan")
        interrupts = state.get("__interrupt__") or ()
        review = interrupts[0].value if interrupts else None
        status: Status
        if review is not None:
            status = "pending_review"
        elif state.get("blocked"):
            status = "blocked"
        else:
            status = "rejected" if state.get("status") == "rejected" else "completed"
        return ChatResult(
            thread_id=thread_id,
            blocked=bool(state.get("blocked")),
            # Nothing is shown to the end user until a reviewer decides.
            answer=None if review is not None else state.get("answer"),
            routing=state.get("decision"),
            guardrails={
                "reasons": state.get("guardrail_reasons", []),
                "flags": state.get("guardrail_flags", []),
            },
            usage=_usage(state),
            mode=mode,
            team={"plan": plan, "results": state.get("results", [])} if plan else None,
            sources=_sources(state.get("knowledge", [])),
            status=status,
            review=review,
            evidence=state.get("evidence"),
            citations=state.get("citations"),
            route_log=state.get("route_log", []),
            decision_record=state.get("decision_record"),
            memory={
                "recalled": len(state.get("memories", [])),
                "stored": len(state.get("remembered", [])),
            },
        )

    async def _after_run(
        self, tenant: str, thread_id: str, subject_id: str | None, actor: str, result: ChatResult
    ) -> None:
        """Book-keeping outside the graph: open the review, write the audit trail. The
        audit keeps metadata only (who, which agents, why), not the conversation."""
        if result.status == "pending_review":
            assert result.review is not None
            await self.reviews.open(tenant, thread_id, result.review, subject_id)
        await self.audit.record(
            tenant,
            actor,
            f"chat.{result.status}",
            f"thread/{thread_id}",
            subject_id=subject_id,
            details={
                "agents": (result.decision_record or {}).get("agents")
                or ((result.review or {}).get("agents"))
                or [],
                "risk": (result.review or result.decision_record or {}).get("risk"),
                "evidence": (result.evidence or {}).get("grade"),
                "flags": result.guardrails["flags"],
            },
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
        subject_id: str | None = None,
        force_review: bool = False,
        actor: str = "api",
    ) -> ChatResult:
        thread_id, inputs, config = await self._prepare(
            question, thread_id, agent_id, agent_ids, mode, tenant, subject_id, force_review
        )
        state = await self.graph.ainvoke(inputs, config)
        result = self._result(thread_id, mode, state)
        await self._after_run(tenant, thread_id, subject_id, actor, result)
        return result

    async def resolve_review(
        self,
        tenant: str,
        thread_id: str,
        *,
        approved: bool,
        reviewer: str,
        feedback: str | None = None,
        edited_answer: str | None = None,
    ) -> ChatResult:
        """Apply a human decision to a paused thread and let the graph finish it."""
        pending = await self.reviews.get(tenant, thread_id)
        if pending is None or pending.status != "pending":
            raise ReviewNotFoundError(thread_id)
        decision = {
            "approved": approved,
            "reviewer": reviewer,
            "feedback": feedback,
            "edited_answer": edited_answer,
        }
        # Claim the review first: two reviewers clicking at once apply one decision.
        if not await self.reviews.resolve(
            tenant, thread_id, "approved" if approved else "rejected", decision
        ):
            raise ReviewNotFoundError(thread_id)
        config = self._config(tenant, thread_id)
        try:
            state = await self.graph.ainvoke(Command(resume=decision), config)
        except Exception:
            # The thread is still paused at its checkpoint: put the review back.
            await self.reviews.open(tenant, thread_id, pending.payload, pending.subject_id)
            raise
        snapshot = await self.graph.aget_state(config)
        mode: Mode = "team" if snapshot.values.get("mode") == "team" else "single"
        result = self._result(thread_id, mode, state)
        await self.audit.record(
            tenant,
            reviewer,
            "review.approved" if approved else "review.rejected",
            f"thread/{thread_id}",
            subject_id=pending.subject_id,
            details={
                "edited": bool(edited_answer),
                "feedback": bool(feedback),
                "risk": pending.payload.get("risk"),
            },
        )
        return result

    async def chat_stream(
        self,
        question: str,
        *,
        thread_id: str | None = None,
        agent_id: str | None = None,
        agent_ids: list[str] | None = None,
        mode: Mode = "single",
        tenant: str = "default",
        subject_id: str | None = None,
        force_review: bool = False,
        actor: str = "api",
    ) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        """Yields `(event, data)` as each graph node finishes: `start`, `guardrails`,
        `knowledge` (if the tenant's documents matched), `routing` or `plan`, one `step`
        per specialist, `review` if the answer was paused for a human, then `done` with
        the full result.
        Validation errors raise before the first event, so callers can still return 4xx."""
        thread_id, inputs, config = await self._prepare(
            question, thread_id, agent_id, agent_ids, mode, tenant, subject_id, force_review
        )

        async def events() -> AsyncIterator[tuple[str, dict[str, Any]]]:
            yield "start", {"thread_id": thread_id, "mode": mode}
            interrupts: tuple[Any, ...] = ()
            async for chunk in self.graph.astream(inputs, config, stream_mode="updates"):
                for node, update in chunk.items():
                    if not update:
                        continue
                    if node == "__interrupt__":
                        interrupts = tuple(update)
                        yield "review", {"status": "pending_review", **interrupts[0].value}
                    elif node == "input_guard":
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
                    elif node == "grade_evidence":
                        yield "evidence", update["evidence"]
                    elif node == "route":
                        yield "routing", update["decision"]
                    elif node == "plan":
                        yield "plan", update["plan"]
                    elif node == "worker":
                        for result in update["results"]:
                            yield "step", result
            snapshot = await self.graph.aget_state(config)
            values = dict(snapshot.values)
            if interrupts:
                values["__interrupt__"] = interrupts
            result = self._result(thread_id, mode, values)
            await self._after_run(tenant, thread_id, subject_id, actor, result)
            yield "done", result.__dict__

        return events()
