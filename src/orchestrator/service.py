"""Composition root shared by the HTTP API, the A2A endpoint, the MCP server and the CLI."""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from langchain_core.runnables import RunnableConfig
from langgraph.types import Command

from orchestrator import packs
from orchestrator import usage as ledger
from orchestrator.answers import Provenance, provenance, sources_view
from orchestrator.auth import PrincipalStore
from orchestrator.campaigns import CampaignService
from orchestrator.catalog import Catalog, load_catalog
from orchestrator.checkpoint import Checkpointer
from orchestrator.clinical_records import ClinicalRecords
from orchestrator.config import Settings
from orchestrator.crm import CrmService
from orchestrator.db import Database, aware, utcnow
from orchestrator.establishment import Professionals
from orchestrator.factories import (  # re-exported for callers of the old location
    build_chunk_store,
    build_embedder,
    build_memory_store,
    build_store,
)
from orchestrator.governance import (
    AuditLog,
    ConsentRegistry,
    ReviewQueue,
    SubjectThreads,
    ThreadLeases,
)
from orchestrator.graph import build_graph
from orchestrator.guardrails import redact_pii
from orchestrator.inbound import InboundService
from orchestrator.insights import InsightsService
from orchestrator.instruments import Instruments
from orchestrator.knowledge import KnowledgeBase
from orchestrator.llm import LLMClient, build_llm
from orchestrator.media_store import build_media_store
from orchestrator.memory import SemanticMemory
from orchestrator.oncall import OnCall
from orchestrator.portal import PatientPortal
from orchestrator.publishing import PublicationService
from orchestrator.remote import A2AClient, discover_all
from orchestrator.router import Router, RoutingDecision
from orchestrator.social import SocialAccounts
from orchestrator.subject_rights import SubjectRights
from orchestrator.telemetry import OPS

log = logging.getLogger(__name__)

Mode = Literal["single", "team"]
Status = Literal["completed", "blocked", "pending_review", "rejected"]


class PendingReviewError(RuntimeError):
    """The thread is paused waiting for a human decision; it cannot take a new message."""


class ReviewNotFoundError(KeyError):
    """No pending review for this thread (never opened, already resolved, other tenant)."""


class ThreadSubjectError(RuntimeError):
    """The thread belongs to another data subject (history must not cross patients)."""


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
    provenance: Provenance = "ai_unreviewed"


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
    # The stages above are what the answer is made of; the total comes from the ledger,
    # which also counts discarded attempts, memory extraction and embeddings (A28).
    entries = ledger.current()
    usage["total"] = ledger.summarize(entries) if entries is not None else _total(parts)
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
        problems = settings.production_problems()
        if problems and not settings.prod_allow_ephemeral:
            raise RuntimeError(
                "APP_ENV=prod with a non-durable configuration:\n- "
                + "\n- ".join(problems)
                + "\nFix these, or set PROD_ALLOW_EPHEMERAL=true for a throwaway demo."
            )
        for problem in problems:
            log.warning("prod running ephemeral (PROD_ALLOW_EPHEMERAL): %s", problem)
        self.catalog = catalog or load_catalog(settings.agents_dir)
        self.llm = llm or build_llm(settings)
        embedder = build_embedder(settings)
        self.router = Router(self.catalog, embedder, build_store(settings), self.llm, settings)
        self.db = Database(settings)
        self.knowledge = KnowledgeBase(embedder, build_chunk_store(settings), settings, self.db)
        self.memory = SemanticMemory(embedder, build_memory_store(settings), self.llm, settings)
        self.checkpointer = Checkpointer(settings)
        self.audit = AuditLog(self.db)
        self.reviews = ReviewQueue(self.db)
        self.leases = ThreadLeases(self.db, timedelta(seconds=settings.thread_lease_seconds))
        self.subject_threads = SubjectThreads(self.db)
        self.consents = ConsentRegistry(self.db, self.audit)
        self.principals = PrincipalStore(self.db, self.audit)
        self.professionals = Professionals(self.db, self.audit, settings)
        self.clinical = ClinicalRecords(self.db, self.audit, self.professionals)
        self.instruments = Instruments(self.db, self.audit)
        self.social = SocialAccounts(self.db, self.audit, self.professionals)
        self.inbound = InboundService(self.db, self.audit, settings)
        self.oncall = OnCall(self.db, self.audit, settings)
        self.publications = PublicationService(
            self.db, self.audit, self.social, build_media_store(settings), settings
        )
        self.crm = CrmService(self.db, self.audit, settings)
        self.insights = InsightsService(self.db, self.crm, self.consents, self.llm, settings)
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
        # Application services with explicit dependencies (audit 2026-10-08, item 7);
        # this object implements the conversation ports they need.
        self.rights = SubjectRights(
            self,
            self.subject_threads,
            self.principals,
            self.consents,
            self.memory,
            self.crm,
            self.clinical,
            self.instruments,
            self.campaigns,
            self.reviews,
            self.audit,
        )
        self.portal = PatientPortal(self, self.crm, self.campaigns, self.consents, settings)
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
        await self.reconcile_reviews()
        self.ready = True

    async def db_ok(self, timeout_s: float = 2.0) -> bool:
        """Readiness probe: the database answers a trivial query within `timeout_s`."""
        try:
            async with asyncio.timeout(timeout_s), self.db.engine.connect() as conn:
                await conn.exec_driver_sql("SELECT 1")
        except Exception:
            log.warning("readiness: database did not answer")
            return False
        return True

    async def refresh_ops_gauges(self) -> dict[str, float]:
        """Backlogs the SLO alerts watch (docs/slo.md), across all tenants: answers held
        for review, the oldest crisis alert nobody has taken, messages not yet sent."""
        from sqlalchemy import func, select

        from orchestrator.campaigns import recipients
        from orchestrator.governance import reviews
        from orchestrator.inbound import channel_alerts

        async with self.db.engine.connect() as conn:
            pending = await conn.scalar(
                select(func.count()).select_from(reviews).where(reviews.c.status == "pending")
            )
            oldest = await conn.scalar(
                select(func.min(channel_alerts.c.created_at)).where(
                    channel_alerts.c.status == "open",
                    channel_alerts.c.acknowledged_at.is_(None),
                )
            )
            outbox = await conn.scalar(
                select(func.count())
                .select_from(recipients)
                .where(recipients.c.status.in_(("pending", "queued")))
            )
        since = aware(oldest)
        age = (utcnow() - since).total_seconds() if since is not None else 0.0
        OPS.update(
            reviews_pending=float(pending or 0),
            alert_oldest_open_age_seconds=max(age, 0.0),
            outbox_pending=float(outbox or 0),
        )
        return dict(OPS)

    async def close(self) -> None:
        self.ready = False
        if self.remote is not None:
            await self.remote.close()
        await self.campaigns.close()
        await self.oncall.close()
        await self.checkpointer.close()
        await self.db.close()

    @staticmethod
    def thread_key(tenant: str, thread_id: str) -> str:
        return f"{tenant}:{thread_id}"

    async def archive_if_clinical(self, tenant: str, thread_id: str, reason: str) -> bool:
        """Before a conversation is deleted: if it had clinical content (a turn the risk
        rules marked clinical, or one a professional reviewed), what the patient was
        shown moves to the clinical record, which is kept. Unreviewed drafts are not
        part of it and go with the conversation."""
        snapshot = await self.graph.aget_state(self._config(tenant, thread_id))
        values = snapshot.values or {}
        subject = values.get("subject_id")
        if not subject:
            return False
        review = await self.reviews.get(tenant, thread_id)
        decided = review is not None and review.status in ("approved", "rejected")
        if not (values.get("clinical") or decided):
            return False
        summary = None
        if decided and review is not None:
            decision = review.decision or {}
            summary = {
                "status": review.status,
                "reviewer": decision.get("reviewer"),
                "resolved_at": review.resolved_at.isoformat() if review.resolved_at else None,
            }
        messages = list(values.get("messages", []))
        if snapshot.next and values.get("sanitized"):
            # Paused for review: the patient's question is part of the record; the AI
            # draft waiting for the professional is not.
            messages.append({"role": "user", "content": values["sanitized"], "unanswered": True})
            summary = {"status": "pending", "reviewer": None, "resolved_at": None}
        await self.crm.archive_conversation(tenant, subject, thread_id, messages, summary, reason)
        return True

    async def delete_thread(
        self, tenant: str, thread_id: str, *, reason: str = "deletion_request", archive: bool = True
    ) -> bool:
        """Erase one conversation of this tenant and its review (the review is removed even
        when the checkpoint is already gone). Clinical content is archived to the clinical
        record first. False if neither existed."""
        key = self.thread_key(tenant, thread_id)
        had_thread = await self.checkpointer.exists(key)
        if had_thread:
            if archive:
                await self.archive_if_clinical(tenant, thread_id, reason)
            await self.checkpointer.delete(key)
        had_review = await self.reviews.get(tenant, thread_id) is not None
        await self.reviews.delete_thread(tenant, thread_id)
        await self.subject_threads.unlink(tenant, thread_id)
        return had_thread or had_review

    async def delete_tenant_threads(self, tenant: str) -> int:
        """Erase every conversation of a tenant (right to erasure / offboarding)."""
        prefix = self.thread_key(tenant, "")
        keys = [k async for k, _ in self.checkpointer.threads() if k.startswith(prefix)]
        for key in keys:
            await self.checkpointer.delete(key)
            await self.reviews.delete_thread(tenant, key.removeprefix(prefix))
            await self.subject_threads.unlink(tenant, key.removeprefix(prefix))
        return len(keys)

    async def start_maintenance(self) -> None:
        """Start only what scheduled jobs (retention) need: the databases. No catalog
        indexing, no remote-agent discovery, no model or embedding call (A31), so the
        job neither costs money nor fails because a provider is down."""
        packs.validate_config(self.settings)
        await self.db.start()
        await self.checkpointer.start()
        self.knowledge.attach()
        self.memory.attach()

    async def retention(self, older_than: timedelta, dry_run: bool = False) -> dict[str, Any]:
        """One retention pass over every store with an expiry; `dry_run` only counts."""
        return {
            "dry_run": dry_run,
            "older_than_days": older_than.days,
            "threads": await self.purge_threads(older_than, dry_run=dry_run),
            "memory_facts": await self.memory.store.purge_expired(utcnow(), dry_run=dry_run),
            "knowledge_leftover_versions": await self.knowledge.reconcile(dry_run=dry_run),
        }

    async def purge_threads(self, older_than: timedelta, dry_run: bool = False) -> int:
        """Retention: delete threads whose latest activity is older than `older_than`
        (clinical ones are archived to the clinical record first)."""
        cutoff = datetime.now(UTC) - older_than
        keys = [k async for k, ts in self.checkpointer.threads() if ts is not None and ts < cutoff]
        if dry_run:
            return len(keys)
        for key in keys:
            # Keys are "tenant:thread"; tenant names cannot contain ":" (config check).
            tenant, _, thread_id = key.partition(":")
            await self.archive_if_clinical(tenant, thread_id, "retention")
            await self.checkpointer.delete(key)
            await self.reviews.delete_thread(tenant, thread_id)  # no orphaned drafts
            await self.subject_threads.unlink(tenant, thread_id)
        return len(keys)

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
        subject_context: str = "",
    ) -> tuple[str, dict[str, Any], RunnableConfig]:
        unknown = [a for a in [agent_id, *(agent_ids or [])] if a and a not in self.catalog.agents]
        if unknown:
            raise KeyError(f"Unknown agent_id: {', '.join(unknown)}")
        thread_id = thread_id or str(uuid.uuid4())
        config = self._config(tenant, thread_id)
        snapshot = await self.graph.aget_state(config)
        # The checkpoint is the source of truth: a paused run blocks new input even if the
        # review row is missing (failed write) or already claimed by a resuming reviewer.
        if await self.reviews.is_open(tenant, thread_id) or snapshot.next:
            # A new input would start a fresh run and orphan the paused one.
            raise PendingReviewError(f"thread {thread_id} is waiting for a human review")
        if snapshot.values and snapshot.values.get("subject_id") != subject_id:
            # One conversation, one data subject: history never crosses patients.
            raise ThreadSubjectError(f"thread {thread_id} belongs to another subject")
        redacted: list[str] = []
        if self.settings.redact_pii:
            # Minimise before anything is checkpointed: the raw input never reaches state.
            question, redacted = redact_pii(question)
        inputs = {
            "question": question,
            "tenant": tenant,
            "mode": mode,
            "agent_override": agent_id,
            "agent_ids": agent_ids or None,
            "subject_id": subject_id,
            "force_review": force_review,
            "subject_context": subject_context,
            "redaction_flags": [f"pii:{label}" for label in redacted],
        }
        return thread_id, inputs, config

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
            sources=sources_view(state.get("knowledge", [])),
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
            provenance=provenance(status, state.get("review")),
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
        subject_context: str = "",
    ) -> ChatResult:
        thread_id = thread_id or str(uuid.uuid4())
        async with self._lease(tenant, thread_id):
            thread_id, inputs, config = await self._prepare(
                question,
                thread_id,
                agent_id,
                agent_ids,
                mode,
                tenant,
                subject_id,
                force_review,
                subject_context,
            )
            if subject_id:
                await self.subject_threads.link(tenant, thread_id, subject_id)
            with ledger.metered():
                state = await self.graph.ainvoke(inputs, config)
                result = self._result(thread_id, mode, state)
            await self._after_run(tenant, thread_id, subject_id, actor, result)
            return result

    @asynccontextmanager
    async def _lease(self, tenant: str, thread_id: str) -> AsyncIterator[None]:
        """One run per conversation at a time, across replicas (audit finding A07)."""
        holder = uuid.uuid4().hex
        await self.leases.acquire(tenant, thread_id, holder)
        try:
            yield
        finally:
            await self.leases.release(tenant, thread_id, holder)

    async def reconcile_reviews(self) -> int:
        """After a crash between claiming a review and finishing its run: a thread still
        paused goes back to pending; a finished one gets its final status."""
        stale = await self.reviews.stale_resolving(
            timedelta(seconds=self.settings.thread_lease_seconds)
        )
        for review in stale:
            snapshot = await self.graph.aget_state(self._config(review.tenant, review.thread_id))
            if snapshot.next:
                await self.reviews.release(review.tenant, review.thread_id)
            else:
                approved = bool((review.decision or {}).get("approved"))
                await self.reviews.finish(
                    review.tenant, review.thread_id, "approved" if approved else "rejected"
                )
            log.warning("reconciled review %s:%s", review.tenant, review.thread_id)
        return len(stale)

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
        async with self._lease(tenant, thread_id):
            return await self._resolve(
                tenant, thread_id, approved, reviewer, feedback, edited_answer
            )

    async def _resolve(
        self,
        tenant: str,
        thread_id: str,
        approved: bool,
        reviewer: str,
        feedback: str | None,
        edited_answer: str | None,
    ) -> ChatResult:
        pending = await self.reviews.get(tenant, thread_id)
        if pending is None or pending.status != "pending":
            raise ReviewNotFoundError(thread_id)

        def minimise(text: str | None) -> str | None:
            # The reviewer's free text is redacted before it is stored anywhere (review
            # queue, checkpoint, decision record), not only in the shown answer.
            if text is None or not self.settings.redact_pii:
                return text
            return redact_pii(text)[0]

        decision = {
            "approved": approved,
            "reviewer": reviewer,
            "feedback": minimise(feedback),
            "edited_answer": minimise(edited_answer),
        }
        # Claim the review first: two reviewers clicking at once apply one decision.
        if not await self.reviews.claim(tenant, thread_id, decision):
            raise ReviewNotFoundError(thread_id)
        config = self._config(tenant, thread_id)
        with ledger.metered():
            try:
                state = await self.graph.ainvoke(Command(resume=decision), config)
            except Exception:
                # Still paused at its checkpoint: the review goes back to pending.
                await self.reviews.release(tenant, thread_id)
                raise
            snapshot = await self.graph.aget_state(config)
            mode: Mode = "team" if snapshot.values.get("mode") == "team" else "single"
            result = self._result(thread_id, mode, state)
        status = "approved" if approved else "rejected"
        async with self.db.engine.begin() as conn:  # final status and its audit event
            await self.reviews.finish(tenant, thread_id, status, conn)
            await self.audit.record_in(
                conn,
                tenant,
                reviewer,
                f"review.{status}",
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
        thread_id = thread_id or str(uuid.uuid4())
        holder = uuid.uuid4().hex
        await self.leases.acquire(tenant, thread_id, holder)
        try:
            thread_id, inputs, config = await self._prepare(
                question, thread_id, agent_id, agent_ids, mode, tenant, subject_id, force_review
            )
            if subject_id:
                await self.subject_threads.link(tenant, thread_id, subject_id)
        except BaseException:
            await self.leases.release(tenant, thread_id, holder)
            raise

        async def events() -> AsyncIterator[tuple[str, dict[str, Any]]]:
            try:
                async for item in run():
                    yield item
            finally:
                await self.leases.release(tenant, thread_id, holder)

        async def run() -> AsyncIterator[tuple[str, dict[str, Any]]]:
            with ledger.metered():
                async for item in stream():
                    yield item

        async def stream() -> AsyncIterator[tuple[str, dict[str, Any]]]:
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
                        yield "knowledge", {"sources": sources_view(update["knowledge"])}
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

    async def thread_state(self, tenant: str, thread_id: str) -> tuple[dict[str, Any], bool]:
        """A conversation's saved values, and whether it is paused for a review (the
        conversation port of SubjectRights and PatientPortal)."""
        snapshot = await self.graph.aget_state(self._config(tenant, thread_id))
        return dict(snapshot.values or {}), bool(snapshot.next)

    async def thread_status(self, tenant: str, thread_id: str) -> dict[str, Any]:
        """Poll a staff conversation, e.g. after its answer was held for review (A32).
        Never the draft: while held, and after a rejection, there is no answer."""
        values, paused = await self.thread_state(tenant, thread_id)
        if not values:
            raise KeyError(thread_id)
        if paused:
            return {
                "thread_id": thread_id,
                "status": "pending_review",
                "answer": None,
                "sources": [],
                "provenance": "ai_pending_review",
            }
        status = "blocked" if values.get("blocked") else (values.get("status") or "completed")
        done = status == "completed"
        return {
            "thread_id": thread_id,
            "status": status,
            "answer": values.get("answer") if done else None,
            "sources": sources_view(values.get("knowledge", [])) if done else [],
            "provenance": provenance(status, values.get("review")),
        }
