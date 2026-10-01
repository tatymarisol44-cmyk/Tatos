"""LangGraph workflow.

    input_guard -> recall_memory -> knowledge -> grade_evidence
        grade_evidence -(weak evidence on a follow-up)-> rewrite_query -> knowledge (retry)
        grade_evidence -> single: route -> specialist
                       -> team:   plan -> worker x N (Send, dependency waves) -> join
                                  -> ... -> synthesize
    -> verify_citations -(invented [n], retries left)-> specialist (regenerate)
    -> output_guard -> risk_score -(high risk)-> review (interrupt: waits for a human)
    -> finalize -> remember -> END

Every decision point is a small route function over typed state, and every node that
decides something appends to `route_log`, so "which branch ran and why" is part of the
result instead of being buried in prompts (docs/adr/0008 and 0009).

Conversation state is checkpointed per `thread_id`: follow-ups keep their history, and a
thread paused for review survives restarts (Postgres checkpointer) until a human resumes
it with `Command(resume=...)`.

A specialist can be a remote A2A agent (another service, any language): it is called over
JSON-RPC instead of the LLM, and if it is unreachable the LLM answers from its card."""

from __future__ import annotations

import logging
import operator
import time
from typing import Annotated, Any, TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Send, interrupt

from orchestrator import evidence
from orchestrator.catalog import AgentSpec, Catalog
from orchestrator.config import Settings
from orchestrator.governance import ConsentRegistry, Purpose
from orchestrator.guardrails import check_input, check_output
from orchestrator.knowledge import Chunk, KnowledgeBase, knowledge_block
from orchestrator.llm import LLMClient, LLMResult
from orchestrator.memory import MemoryFact, SemanticMemory, memory_block
from orchestrator.packs import pack_for
from orchestrator.remote import A2AClient, RemoteAgentError, context_id
from orchestrator.risk import assess
from orchestrator.router import Router
from orchestrator.team import (
    Planner,
    fallback_synthesis,
    ready_steps,
    synthesis_messages,
    worker_messages,
)
from orchestrator.telemetry import BLOCKED, LATENCY, ROUTED, TOKENS, tracer

log = logging.getLogger(__name__)

WITHHELD = (
    "This answer was reviewed by a member of our team and was not approved, so it is not "
    "shown. Someone from the team will follow up with you directly."
)


def _reset_or_extend(
    existing: list[dict[str, Any]] | None, new: list[dict[str, Any]] | None
) -> list[dict[str, Any]]:
    """Accumulates per-turn lists (parallel worker results, route log); `None` resets them
    at the start of a turn (a plain `operator.add` would carry them over between turns)."""
    if new is None:
        return []
    return [*(existing or []), *new]


class OrchestratorState(TypedDict, total=False):
    question: str
    tenant: str
    mode: str
    agent_override: str | None
    agent_ids: list[str] | None
    subject_id: str | None
    force_review: bool
    subject_context: str
    # PII the service redacted before the run (the raw input never enters state).
    redaction_flags: list[str]
    messages: Annotated[list[dict[str, str]], operator.add]
    sanitized: str
    blocked: bool
    guardrail_reasons: list[str]
    guardrail_flags: list[str]
    memories: list[dict[str, Any]]
    retrieval_query: str
    retrieval_attempts: int
    knowledge: list[dict[str, Any]]
    evidence: dict[str, Any] | None
    decision: dict[str, Any] | None
    plan: dict[str, Any] | None
    results: Annotated[list[dict[str, Any]], _reset_or_extend]
    answer: str | None
    answer_usage: dict[str, Any] | None
    citations: dict[str, Any] | None
    citation_retries: int
    citation_feedback: str | None
    risk: dict[str, Any] | None
    review: dict[str, Any] | None
    status: str
    decision_record: dict[str, Any] | None
    remembered: list[dict[str, Any]]
    route_log: Annotated[list[dict[str, Any]], _reset_or_extend]
    started_at: float


class WorkerInput(TypedDict):
    step: dict[str, Any]
    question: str
    context: list[dict[str, Any]]
    knowledge: str


def _count_tokens(result: LLMResult) -> None:
    TOKENS.add(result.input_tokens, {"type": "input", "model": result.model})
    TOKENS.add(result.output_tokens, {"type": "output", "model": result.model})


def _log(node: str, decision: str, **detail: Any) -> list[dict[str, Any]]:
    return [{"node": node, "decision": decision, **detail}]


def build_graph(
    catalog: Catalog,
    router: Router,
    llm: LLMClient,
    settings: Settings,
    knowledge_base: KnowledgeBase | None = None,
    checkpointer: BaseCheckpointSaver[Any] | None = None,
    remote: A2AClient | None = None,
    memory: SemanticMemory | None = None,
    consents: ConsentRegistry | None = None,
) -> CompiledStateGraph[Any]:
    planner = Planner(catalog, router, llm, settings)

    async def answer(
        agent: AgentSpec,
        messages: list[dict[str, str]],
        remote_text: str,
        config: RunnableConfig,
    ) -> tuple[LLMResult, dict[str, str] | None]:
        """Ask a local agent (LLM + its prompt) or a remote A2A agent. A failed remote
        call degrades to the LLM playing the agent from its card, and says so."""
        if not agent.remote_url or remote is None:
            return await llm.complete(messages, model=settings.llm_model), None
        thread = str((config.get("configurable") or {}).get("thread_id", ""))
        with tracer().start_as_current_span("agent.remote") as span:
            span.set_attribute("agent.id", agent.id)
            try:
                text = await remote.send(agent, remote_text, context_id(thread, agent.id))
            except RemoteAgentError as exc:
                span.record_exception(exc)
                log.warning("remote agent %s failed (%s); answering locally", agent.id, exc)
            else:
                return LLMResult(text, f"a2a/{agent.id}"), {"status": "ok"}
        result = await llm.complete(messages, model=settings.llm_model)
        return result, {"status": "fallback", "error": "remote agent unavailable"}

    def context_block(state: OrchestratorState) -> str:
        """Recalled memory and knowledge excerpts (graded), as delimited untrusted data."""
        parts: list[str] = []
        if state.get("subject_context"):
            parts.append(state["subject_context"])
        facts = [MemoryFact(**m) for m in state.get("memories", [])]
        if facts:
            parts.append(memory_block(facts))
        chunks = [Chunk(**c) for c in state.get("knowledge", [])]
        if chunks:
            block = knowledge_block(chunks)
            if (state.get("evidence") or {}).get("grade") == "weak":
                block = (
                    "Note: these excerpts are only loosely related to the request. Do not "
                    "present them as confirming anything they do not state explicitly.\n" + block
                )
            parts.append(block)
        return "\n\n".join(parts)

    def scrub(text: str) -> str:
        """Redact model output where it is produced: intermediate results are
        checkpointed and streamed (team steps) before the final output guard runs."""
        return check_output(text, redact=settings.redact_pii).text

    def with_context(block: str, text: str) -> str:
        """Prepend the context block (if any) to a user message."""
        return f"{block}\n\n{text}" if block else text

    def history(state: OrchestratorState) -> list[dict[str, str]]:
        return state.get("messages", [])[-settings.history_max_messages :]

    def agents_used(state: OrchestratorState) -> list[AgentSpec]:
        decision = state.get("decision")
        if state.get("plan"):
            ids = [r["agent_id"] for r in state.get("results", []) if r.get("output")]
        elif decision:
            ids = [decision["agent_id"]]
        else:
            ids = []
        return [catalog.agents[i] for i in ids if i in catalog.agents]

    async def consented(state: OrchestratorState, purpose: Purpose) -> bool:
        subject = state.get("subject_id")
        if not subject or consents is None:
            return False
        return await consents.has(state.get("tenant", ""), subject, purpose)

    # --- input ---------------------------------------------------------------
    async def input_guard(state: OrchestratorState) -> dict[str, Any]:
        result = check_input(
            state["question"],
            max_chars=settings.max_input_chars,
            injection_action=settings.injection_action,
            redact=settings.redact_pii,
        )
        if not result.allowed:
            BLOCKED.add(1, {"reason": result.reasons[0], "tenant": state.get("tenant", "")})
        # Everything below is per turn: reset it so a thread's previous turn never leaks in.
        return {
            "sanitized": result.text,
            "blocked": not result.allowed,
            "guardrail_reasons": result.reasons,
            "guardrail_flags": sorted({*state.get("redaction_flags", []), *result.flags}),
            "memories": [],
            "retrieval_query": result.text,
            "retrieval_attempts": 0,
            "knowledge": [],
            "evidence": None,
            "decision": None,
            "plan": None,
            "results": None,
            "answer": None,
            "answer_usage": None,
            "citations": None,
            "citation_retries": 0,
            "citation_feedback": None,
            "risk": None,
            "review": None,
            "status": "running" if result.allowed else "blocked",
            "decision_record": None,
            "remembered": [],
            "route_log": None,
            "started_at": time.perf_counter(),
        }

    def after_guard(state: OrchestratorState) -> str:
        return END if state["blocked"] else "recall_memory"

    # --- long-term memory of the data subject ---------------------------------
    async def recall_memory(state: OrchestratorState) -> dict[str, Any]:
        subject = state.get("subject_id")
        if memory is None or not settings.memory_enabled or not subject:
            return {}
        if not await consented(state, Purpose.MEMORY):
            return {"route_log": _log("recall_memory", "skipped", reason="no memory consent")}
        try:
            facts = await memory.recall(state.get("tenant", ""), subject, state["sanitized"])
        except Exception:  # memory is an enhancement: answer without it rather than fail
            log.exception("memory recall failed; answering without it")
            return {"route_log": _log("recall_memory", "failed")}
        return {
            "memories": [f.to_dict() for f in facts],
            "route_log": _log("recall_memory", "recalled", facts=len(facts)),
        }

    # --- RAG over the tenant's documents, with evidence gating -----------------
    async def knowledge(state: OrchestratorState) -> dict[str, Any]:
        attempt = state.get("retrieval_attempts", 0) + 1
        if knowledge_base is None or not settings.knowledge_enabled:
            return {"retrieval_attempts": attempt}
        try:
            chunks = await knowledge_base.search(
                state.get("tenant", ""), state.get("retrieval_query") or state["sanitized"]
            )
        except Exception:  # retrieval is an enhancement: answer without it rather than fail
            log.exception("knowledge search failed; answering without company context")
            return {"retrieval_attempts": attempt}
        found = [c.to_dict() for c in chunks]
        # A retry uses the query rewritten with the conversation's context, which states
        # the intent better than a bare follow-up ("how many days?") even when the bare
        # one happened to score higher on some other document. Keep the first attempt
        # only when the retry finds nothing.
        return {
            "knowledge": found or state.get("knowledge", []),
            "retrieval_attempts": attempt,
        }

    async def grade_evidence(state: OrchestratorState) -> dict[str, Any]:
        chunks = state.get("knowledge", [])
        grade = evidence.grade(chunks, settings.knowledge_strong_score)
        top = round(max((c["score"] for c in chunks), default=0.0), 4)
        info = {"grade": grade, "top_score": top, "attempts": state.get("retrieval_attempts", 1)}
        return {"evidence": info, "route_log": _log("grade_evidence", grade, top_score=top)}

    def after_grade(state: OrchestratorState) -> str:
        if (
            (state.get("evidence") or {}).get("grade") == "weak"
            and state.get("retrieval_attempts", 1) < settings.knowledge_max_attempts
            and evidence.contextual_query(history(state), state["sanitized"]) is not None
        ):
            return "rewrite_query"
        return "plan" if state.get("mode") == "team" else "route"

    async def rewrite_query(state: OrchestratorState) -> dict[str, Any]:
        query = evidence.contextual_query(history(state), state["sanitized"])
        return {
            "retrieval_query": query or state["sanitized"],
            "route_log": _log("rewrite_query", "retry", reason="weak evidence on a follow-up"),
        }

    # --- single-agent path ---------------------------------------------------
    async def route(state: OrchestratorState) -> dict[str, Any]:
        decision = await router.route(state["sanitized"], state.get("agent_override"))
        ROUTED.add(1, {"agent_id": decision.agent_id, "method": decision.method})
        return {
            "decision": decision.to_dict(),
            "route_log": _log(
                "route", decision.agent_id, method=decision.method, confidence=decision.confidence
            ),
        }

    async def specialist(state: OrchestratorState, config: RunnableConfig) -> dict[str, Any]:
        decision = state["decision"]
        assert decision is not None
        agent = catalog.agents[decision["agent_id"]]
        block = context_block(state)
        messages = [
            {"role": "system", "content": agent.system_prompt},
            *history(state),
            {"role": "user", "content": with_context(block, state["sanitized"])},
        ]
        feedback = state.get("citation_feedback")
        if feedback:
            messages.append({"role": "user", "content": feedback})
        # Remote agents keep their own history (per contextId) and only see tenant
        # documents when REMOTE_SHARE_KNOWLEDGE allows it.
        shared = block if settings.remote_share_knowledge else ""
        with tracer().start_as_current_span("agent.answer") as span:
            span.set_attribute("agent.id", agent.id)
            span.set_attribute("agent.division", agent.division)
            result, remote_info = await answer(
                agent, messages, with_context(shared, state["sanitized"]), config
            )
        _count_tokens(result)
        update: dict[str, Any] = {"answer": scrub(result.text), "answer_usage": result.usage()}
        if remote_info:
            update["decision"] = {**decision, "remote": remote_info}
        return update

    # --- team path -----------------------------------------------------------
    async def plan(state: OrchestratorState) -> dict[str, Any]:
        team_plan = await planner.plan(
            state["sanitized"], agent_ids=state.get("agent_ids"), history=history(state)
        )
        for step in team_plan.steps:
            ROUTED.add(1, {"agent_id": step.agent_id, "method": f"team-{team_plan.method}"})
        return {
            "plan": team_plan.to_dict(),
            "route_log": _log(
                "plan", team_plan.method, agents=[s.agent_id for s in team_plan.steps]
            ),
        }

    def next_wave(state: OrchestratorState) -> list[Send] | str:
        assert state["plan"] is not None
        by_step = {r["step_id"]: r for r in state.get("results", [])}
        ready = ready_steps(state["plan"]["steps"], set(by_step))
        if not ready:
            return "synthesize"
        return [
            Send(
                "worker",
                {
                    "step": step,
                    "question": state["sanitized"],
                    "context": [by_step[d] for d in step["depends_on"]],
                    "knowledge": context_block(state),
                },
            )
            for step in ready
        ]

    async def worker(payload: WorkerInput, config: RunnableConfig) -> dict[str, Any]:
        step = payload["step"]
        agent = catalog.agents[step["agent_id"]]
        out: dict[str, Any] = {
            "step_id": step["id"],
            "agent_id": agent.id,
            "agent_name": agent.name,
            "task": step["task"],
            "output": None,
            "error": None,
            "usage": None,
        }
        messages = worker_messages(
            agent.system_prompt,
            payload["question"],
            step["task"],
            payload["context"],
            settings.team_context_chars,
        )
        task_text = messages[-1]["content"]
        messages[-1]["content"] = with_context(payload["knowledge"], task_text)
        shared = payload["knowledge"] if settings.remote_share_knowledge else ""
        with tracer().start_as_current_span("team.worker") as span:
            span.set_attribute("agent.id", agent.id)
            span.set_attribute("team.step_id", step["id"])
            started = time.perf_counter()
            try:
                result, remote_info = await answer(
                    agent, messages, with_context(shared, task_text), config
                )
            except Exception as exc:  # one failed specialist must not sink the team
                span.record_exception(exc)
                out["error"] = type(exc).__name__
            else:
                _count_tokens(result)
                out["output"] = scrub(result.text)
                out["usage"] = result.usage()
                if remote_info:
                    out["remote"] = remote_info
            out["duration_s"] = round(time.perf_counter() - started, 3)
        return {"results": [out]}

    async def join(state: OrchestratorState) -> dict[str, Any]:
        return {}

    async def synthesize(state: OrchestratorState) -> dict[str, Any]:
        assert state["plan"] is not None
        order = {s["id"]: i for i, s in enumerate(state["plan"]["steps"])}
        results = sorted(state.get("results", []), key=lambda r: order[r["step_id"]])
        ok = [r for r in results if r.get("output")]
        text: str
        usage: dict[str, Any] | None = None
        if len(results) == 1 and ok:
            # One specialist was enough: no extra synthesis call.
            text = ok[0]["output"]
        elif not ok:
            text = fallback_synthesis(results)
        else:
            messages = synthesis_messages(
                state["sanitized"], results, history(state), settings.team_context_chars
            )
            # Same numbered block the specialists saw, so their [n] citations stay valid.
            messages[-1]["content"] = with_context(context_block(state), messages[-1]["content"])
            with tracer().start_as_current_span("team.synthesize") as span:
                span.set_attribute("team.contributions", len(ok))
                try:
                    result = await llm.complete(messages, model=settings.llm_model)
                except Exception as exc:
                    span.record_exception(exc)
                    text = fallback_synthesis(results)
                else:
                    _count_tokens(result)
                    text, usage = scrub(result.text), result.usage()
        return {"answer": text, "answer_usage": usage}

    # --- citations: an [n] must point to a retrieved excerpt -------------------
    async def verify_citations(state: OrchestratorState) -> dict[str, Any]:
        n_sources = len(state.get("knowledge", []))
        text = state.get("answer") or ""
        # Without retrieved excerpts nobody asked for [n]: brackets are not citations.
        used, invalid = evidence.check_citations(text, n_sources) if n_sources else ([], [])
        info = {"used": used, "invalid": invalid}
        retries = state.get("citation_retries", 0)
        if invalid and state.get("mode") != "team" and retries < settings.citation_max_retries:
            cited = ", ".join(f"[{n}]" for n in invalid)
            return {
                "citations": info,
                "citation_retries": retries + 1,
                "citation_feedback": (
                    f"Your previous answer cited {cited}, which do not exist. Answer again "
                    f"and only cite excerpts [1] to [{n_sources}], or none."
                ),
                "route_log": _log("verify_citations", "regenerate", invalid=invalid),
            }
        update: dict[str, Any] = {
            "citations": info,
            "citation_feedback": None,
            "route_log": _log("verify_citations", "stripped" if invalid else "ok", used=used),
        }
        if invalid:  # fail closed: never show a reference to a source that does not exist
            update["answer"] = evidence.strip_citations(text, invalid)
            update["guardrail_flags"] = [*state.get("guardrail_flags", []), "citation:invalid"]
        return update

    def after_verify(state: OrchestratorState) -> str:
        return "specialist" if state.get("citation_feedback") else "output_guard"

    async def output_guard(state: OrchestratorState) -> dict[str, Any]:
        result = check_output(state.get("answer") or "", redact=settings.redact_pii)
        LATENCY.record(
            time.perf_counter() - state["started_at"],
            {"tenant": state.get("tenant", ""), "mode": state.get("mode") or "single"},
        )
        return {
            "answer": result.text,
            "guardrail_flags": [*state.get("guardrail_flags", []), *result.flags],
        }

    # --- ERM: risk scoring and human review -----------------------------------
    async def risk_score(state: OrchestratorState) -> dict[str, Any]:
        assessment = assess(
            pack_for(settings, state.get("tenant", "")),
            question=state["sanitized"],
            answer=state.get("answer") or "",
            divisions=[a.division for a in agents_used(state)],
            flags=state.get("guardrail_flags", []),
            forced=bool(state.get("force_review")),
        )
        return {
            "risk": assessment.to_dict(),
            "route_log": _log("risk_score", assessment.level, reasons=assessment.reasons),
        }

    def after_risk(state: OrchestratorState) -> str:
        high = (state.get("risk") or {}).get("level") == "high"
        return "review" if high and settings.review_enabled else "finalize"

    async def review(state: OrchestratorState) -> dict[str, Any]:
        # Execution stops here and the checkpointer keeps the thread until a human
        # resumes it; on resume this node runs again and `interrupt` returns the decision.
        decision: dict[str, Any] = interrupt(
            {
                "draft_answer": state.get("answer"),
                "risk": state.get("risk"),
                "question": state["sanitized"],
                "agents": [a.id for a in agents_used(state)],
                "sources": [
                    {"n": i, "title": c["title"], "doc_id": c["doc_id"]}
                    for i, c in enumerate(state.get("knowledge", []), 1)
                ],
                "evidence": state.get("evidence"),
            }
        )
        verdict = "approved" if decision.get("approved") else "rejected"
        return {
            "review": decision,
            "route_log": _log("review", verdict, reviewer=decision.get("reviewer")),
        }

    async def finalize(state: OrchestratorState) -> dict[str, Any]:
        text = state.get("answer") or ""
        decision = state.get("review")
        status = "completed"
        flags = list(state.get("guardrail_flags", []))
        citations = state.get("citations")
        if decision is not None:
            if not decision.get("approved"):
                text, status = WITHHELD, "rejected"
            elif decision.get("edited_answer"):
                # The reviewer's text goes through the same output checks as the model's,
                # citations included: the metadata must describe the text actually shown.
                checked = check_output(decision["edited_answer"], redact=settings.redact_pii)
                text, flags = checked.text, [*flags, *checked.flags]
                n_sources = len(state.get("knowledge", []))
                used, invalid = evidence.check_citations(text, n_sources)
                if invalid:
                    text = evidence.strip_citations(text, invalid)
                    flags.append("citation:invalid")
                citations = {"used": used, "invalid": invalid}
        record = {
            "status": status,
            "risk": state.get("risk"),
            "review": decision,
            "agents": [a.id for a in agents_used(state)],
            "evidence": state.get("evidence"),
            "citations": citations,
        }
        return {
            "answer": text,
            "status": status,
            "citations": citations,
            "guardrail_flags": flags,
            "decision_record": record,
            # History keeps what the user was actually shown, not a rejected draft.
            "messages": [
                {"role": "user", "content": state["sanitized"]},
                {"role": "assistant", "content": text},
            ],
        }

    async def remember(state: OrchestratorState, config: RunnableConfig) -> dict[str, Any]:
        subject = state.get("subject_id")
        if (
            memory is None
            or not settings.memory_enabled
            or not subject
            or state.get("status") != "completed"
            or not await consented(state, Purpose.MEMORY)
        ):
            return {}
        tenant = state.get("tenant", "")
        thread = str((config.get("configurable") or {}).get("thread_id", ""))
        try:
            facts = await memory.extract(state["sanitized"], state.get("answer") or "")
            stored = await memory.remember(
                tenant,
                subject,
                facts,
                thread,
                allow_clinical=pack_for(settings, tenant).memory.allow_clinical,
            )
        except Exception:  # never lose the answer because memory failed
            log.exception("memory write failed")
            return {"route_log": _log("remember", "failed")}
        return {
            "remembered": [f.to_dict() for f in stored],
            "route_log": _log("remember", "stored", facts=len(stored)),
        }

    builder = StateGraph(OrchestratorState)
    builder.add_node("input_guard", input_guard)
    builder.add_node("recall_memory", recall_memory)
    builder.add_node("knowledge", knowledge)
    builder.add_node("grade_evidence", grade_evidence)
    builder.add_node("rewrite_query", rewrite_query)
    builder.add_node("route", route)
    builder.add_node("specialist", specialist)
    builder.add_node("plan", plan)
    builder.add_node("worker", worker, input_schema=WorkerInput)  # type: ignore[arg-type]
    builder.add_node("join", join)
    builder.add_node("synthesize", synthesize)
    builder.add_node("verify_citations", verify_citations)
    builder.add_node("output_guard", output_guard)
    builder.add_node("risk_score", risk_score)
    builder.add_node("review", review)
    builder.add_node("finalize", finalize)
    builder.add_node("remember", remember)

    builder.add_edge(START, "input_guard")
    builder.add_conditional_edges("input_guard", after_guard, ["recall_memory", END])
    builder.add_edge("recall_memory", "knowledge")
    builder.add_edge("knowledge", "grade_evidence")
    builder.add_conditional_edges("grade_evidence", after_grade, ["rewrite_query", "route", "plan"])
    builder.add_edge("rewrite_query", "knowledge")
    builder.add_edge("route", "specialist")
    builder.add_edge("specialist", "verify_citations")
    # Each wave of workers converges on `join` (which runs once per wave); it either
    # sends the next ready steps or moves on to synthesis.
    builder.add_conditional_edges("plan", next_wave, ["worker", "synthesize"])
    builder.add_edge("worker", "join")
    builder.add_conditional_edges("join", next_wave, ["worker", "synthesize"])
    builder.add_edge("synthesize", "verify_citations")
    builder.add_conditional_edges("verify_citations", after_verify, ["specialist", "output_guard"])
    builder.add_edge("output_guard", "risk_score")
    builder.add_conditional_edges("risk_score", after_risk, ["review", "finalize"])
    builder.add_edge("review", "finalize")
    builder.add_edge("finalize", "remember")
    builder.add_edge("remember", END)
    return builder.compile(checkpointer=checkpointer or InMemorySaver())
