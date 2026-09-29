"""LangGraph workflow. After the input guard, `knowledge` retrieves the tenant's own
documents (RAG), then the request takes one of two paths:

- single: route -> specialist                       (one agent answers)
- team:   plan -> worker x N (Send, in dependency waves) -> join -> ... -> synthesize

Both end in output_guard. Conversation state is checkpointed per `thread_id`, so
follow-up questions keep their history whichever path or specialists they take."""

from __future__ import annotations

import logging
import operator
import time
from typing import Annotated, Any, TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Send

from orchestrator.catalog import Catalog
from orchestrator.config import Settings
from orchestrator.guardrails import check_input, check_output
from orchestrator.knowledge import Chunk, KnowledgeBase, knowledge_block
from orchestrator.llm import LLMClient, LLMResult
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


def _step_results(
    existing: list[dict[str, Any]] | None, new: list[dict[str, Any]] | None
) -> list[dict[str, Any]]:
    """Accumulates parallel worker results; `None` resets them at the start of a turn
    (a plain `operator.add` would carry results over between turns of a thread)."""
    if new is None:
        return []
    return [*(existing or []), *new]


class OrchestratorState(TypedDict, total=False):
    question: str
    tenant: str
    mode: str
    agent_override: str | None
    agent_ids: list[str] | None
    messages: Annotated[list[dict[str, str]], operator.add]
    sanitized: str
    blocked: bool
    guardrail_reasons: list[str]
    guardrail_flags: list[str]
    knowledge: list[dict[str, Any]]
    decision: dict[str, Any] | None
    plan: dict[str, Any] | None
    results: Annotated[list[dict[str, Any]], _step_results]
    answer: str | None
    answer_usage: dict[str, Any] | None
    started_at: float


class WorkerInput(TypedDict):
    step: dict[str, Any]
    question: str
    context: list[dict[str, Any]]
    knowledge: str


def _count_tokens(result: LLMResult) -> None:
    TOKENS.add(result.input_tokens, {"type": "input", "model": result.model})
    TOKENS.add(result.output_tokens, {"type": "output", "model": result.model})


def build_graph(
    catalog: Catalog,
    router: Router,
    llm: LLMClient,
    settings: Settings,
    knowledge_base: KnowledgeBase | None = None,
) -> CompiledStateGraph[Any]:
    planner = Planner(catalog, router, llm, settings)

    def context_block(state: OrchestratorState) -> str:
        chunks = [Chunk(**c) for c in state.get("knowledge", [])]
        return knowledge_block(chunks) if chunks else ""

    def with_context(block: str, text: str) -> str:
        """Prepend the knowledge block (if any) to a user message."""
        return f"{block}\n\n{text}" if block else text

    def history(state: OrchestratorState) -> list[dict[str, str]]:
        return state.get("messages", [])[-settings.history_max_messages :]

    def turn(state: OrchestratorState, answer: str) -> list[dict[str, str]]:
        return [
            {"role": "user", "content": state["sanitized"]},
            {"role": "assistant", "content": answer},
        ]

    async def input_guard(state: OrchestratorState) -> dict[str, Any]:
        result = check_input(
            state["question"],
            max_chars=settings.max_input_chars,
            injection_action=settings.injection_action,
            redact=settings.redact_pii,
        )
        if not result.allowed:
            BLOCKED.add(1, {"reason": result.reasons[0], "tenant": state.get("tenant", "")})
        return {
            "sanitized": result.text,
            "blocked": not result.allowed,
            "guardrail_reasons": result.reasons,
            "guardrail_flags": result.flags,
            "knowledge": [],
            "decision": None,
            "plan": None,
            "results": None,
            "answer": None,
            "answer_usage": None,
            "started_at": time.perf_counter(),
        }

    def after_guard(state: OrchestratorState) -> str:
        return END if state["blocked"] else "knowledge"

    # --- RAG over the tenant's documents -------------------------------------
    async def knowledge(state: OrchestratorState) -> dict[str, Any]:
        if knowledge_base is None or not settings.knowledge_enabled:
            return {}
        try:
            chunks = await knowledge_base.search(state.get("tenant", ""), state["sanitized"])
        except Exception:  # retrieval is an enhancement: answer without it rather than fail
            log.exception("knowledge search failed; answering without company context")
            return {}
        return {"knowledge": [c.to_dict() for c in chunks]}

    def after_knowledge(state: OrchestratorState) -> str:
        return "plan" if state.get("mode") == "team" else "route"

    # --- single-agent path ---------------------------------------------------
    async def route(state: OrchestratorState) -> dict[str, Any]:
        decision = await router.route(state["sanitized"], state.get("agent_override"))
        ROUTED.add(1, {"agent_id": decision.agent_id, "method": decision.method})
        return {"decision": decision.to_dict()}

    async def specialist(state: OrchestratorState) -> dict[str, Any]:
        decision = state["decision"]
        assert decision is not None
        agent = catalog.agents[decision["agent_id"]]
        messages = [
            {"role": "system", "content": agent.system_prompt},
            *history(state),
            {"role": "user", "content": with_context(context_block(state), state["sanitized"])},
        ]
        with tracer().start_as_current_span("agent.answer") as span:
            span.set_attribute("agent.id", agent.id)
            span.set_attribute("agent.division", agent.division)
            result = await llm.complete(messages, model=settings.llm_model)
        _count_tokens(result)
        return {
            "answer": result.text,
            "answer_usage": result.usage(),
            "messages": turn(state, result.text),
        }

    # --- team path -----------------------------------------------------------
    async def plan(state: OrchestratorState) -> dict[str, Any]:
        team_plan = await planner.plan(
            state["sanitized"], agent_ids=state.get("agent_ids"), history=history(state)
        )
        for step in team_plan.steps:
            ROUTED.add(1, {"agent_id": step.agent_id, "method": f"team-{team_plan.method}"})
        return {"plan": team_plan.to_dict()}

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

    async def worker(payload: WorkerInput) -> dict[str, Any]:
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
        messages[-1]["content"] = with_context(payload["knowledge"], messages[-1]["content"])
        with tracer().start_as_current_span("team.worker") as span:
            span.set_attribute("agent.id", agent.id)
            span.set_attribute("team.step_id", step["id"])
            started = time.perf_counter()
            try:
                result = await llm.complete(messages, model=settings.llm_model)
            except Exception as exc:  # one failed specialist must not sink the team
                span.record_exception(exc)
                out["error"] = type(exc).__name__
            else:
                _count_tokens(result)
                out["output"] = result.text
                out["usage"] = result.usage()
            out["duration_s"] = round(time.perf_counter() - started, 3)
        return {"results": [out]}

    async def join(state: OrchestratorState) -> dict[str, Any]:
        return {}

    async def synthesize(state: OrchestratorState) -> dict[str, Any]:
        assert state["plan"] is not None
        order = {s["id"]: i for i, s in enumerate(state["plan"]["steps"])}
        results = sorted(state.get("results", []), key=lambda r: order[r["step_id"]])
        ok = [r for r in results if r.get("output")]
        answer: str
        usage: dict[str, Any] | None = None
        if len(results) == 1 and ok:
            # One specialist was enough: no extra synthesis call.
            answer = ok[0]["output"]
        elif not ok:
            answer = fallback_synthesis(results)
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
                    answer = fallback_synthesis(results)
                else:
                    _count_tokens(result)
                    answer, usage = result.text, result.usage()
        return {"answer": answer, "answer_usage": usage, "messages": turn(state, answer)}

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

    builder = StateGraph(OrchestratorState)
    builder.add_node("input_guard", input_guard)
    builder.add_node("knowledge", knowledge)
    builder.add_node("route", route)
    builder.add_node("specialist", specialist)
    builder.add_node("plan", plan)
    builder.add_node("worker", worker, input_schema=WorkerInput)  # type: ignore[arg-type]
    builder.add_node("join", join)
    builder.add_node("synthesize", synthesize)
    builder.add_node("output_guard", output_guard)
    builder.add_edge(START, "input_guard")
    builder.add_conditional_edges("input_guard", after_guard, ["knowledge", END])
    builder.add_conditional_edges("knowledge", after_knowledge, ["route", "plan"])
    builder.add_edge("route", "specialist")
    builder.add_edge("specialist", "output_guard")
    # Each wave of workers converges on `join` (which runs once per wave); it either
    # sends the next ready steps or moves on to synthesis.
    builder.add_conditional_edges("plan", next_wave, ["worker", "synthesize"])
    builder.add_edge("worker", "join")
    builder.add_conditional_edges("join", next_wave, ["worker", "synthesize"])
    builder.add_edge("synthesize", "output_guard")
    builder.add_edge("output_guard", END)
    return builder.compile(checkpointer=InMemorySaver())
