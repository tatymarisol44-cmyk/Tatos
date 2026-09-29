"""Team orchestration: a planner splits a request into subtasks, each assigned to a
different specialist; the graph runs them in dependency waves and a synthesizer merges
the results.

Like the router, the planner may only pick agents that retrieval surfaced, and any
invalid plan (bad JSON, unknown ids, forward/cyclic dependencies) falls back to a
retrieval-built plan, so team mode never fails closed."""

from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from orchestrator.catalog import Catalog
from orchestrator.config import Settings
from orchestrator.llm import LLMClient, Message
from orchestrator.router import Candidate, Router, _parse_json
from orchestrator.telemetry import tracer

log = logging.getLogger(__name__)

PLANNER_PROMPT = """You are the PLANNER of an AI agency. Split the user's request into at most
{max_steps} subtasks and assign each one to the best specialist from the candidate list.
Use a single step when one specialist is enough; never assign two steps the same work.
A step may depend on earlier steps when it needs their output; independent steps run in
parallel. The request may be in any language; write tasks in that language.
Only use ids from the candidate list. Reply with JSON only:
{{"steps": [{{"id": "s1", "agent_id": "<id>", "task": "<instruction>", "depends_on": []}}],
 "reasoning": "<one short sentence>"}}"""

SYNTHESIZER_PROMPT = """You are the SYNTHESIZER of an AI agency. Several specialists each worked
on part of the user's request. Merge their contributions into one coherent answer in the
user's language: resolve contradictions, remove repetition, keep concrete details and
next steps, and mention which specialist a recommendation comes from when it helps. Keep
citation markers such as [1] that refer to the company's knowledge base."""

_STEP_ID = re.compile(r"^[\w-]{1,32}$")


@dataclass
class PlanStep:
    id: str
    agent_id: str
    agent_name: str
    task: str
    depends_on: list[str] = field(default_factory=list)


@dataclass
class Plan:
    steps: list[PlanStep]
    method: Literal["llm", "retrieval", "override"]
    reasoning: str = ""
    candidates: list[Candidate] = field(default_factory=list)
    usage: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def validate_steps(
    data: dict[str, Any], catalog: Catalog, allowed: set[str], max_steps: int
) -> list[PlanStep]:
    """Keep the valid prefix-consistent steps: known agent, non-empty task, unique id, and
    dependencies only on *earlier* kept steps (which also rules out cycles)."""
    raw = data.get("steps")
    if not isinstance(raw, list):
        return []
    steps: list[PlanStep] = []
    seen: set[str] = set()
    for item in raw:
        if len(steps) >= max_steps:
            break
        if not isinstance(item, dict):
            continue
        agent_id = str(item.get("agent_id", ""))
        task = str(item.get("task", "")).strip()[:2000]
        step_id = str(item.get("id") or f"s{len(steps) + 1}")
        if agent_id not in allowed or not task or step_id in seen or not _STEP_ID.match(step_id):
            continue
        deps = item.get("depends_on") or []
        if not isinstance(deps, list) or any(str(d) not in seen for d in deps):
            continue
        seen.add(step_id)
        steps.append(
            PlanStep(
                id=step_id,
                agent_id=agent_id,
                agent_name=catalog.agents[agent_id].name,
                task=task,
                depends_on=list(dict.fromkeys(str(d) for d in deps)),
            )
        )
    return steps


class Planner:
    def __init__(
        self, catalog: Catalog, router: Router, llm: LLMClient, settings: Settings
    ) -> None:
        self.catalog = catalog
        self.router = router
        self.llm = llm
        self.settings = settings

    async def plan(
        self,
        question: str,
        *,
        agent_ids: list[str] | None = None,
        history: list[Message] | None = None,
    ) -> Plan:
        with tracer().start_as_current_span("team.plan") as span:
            plan = await self._plan(question, agent_ids, history or [])
            span.set_attribute("team.method", plan.method)
            span.set_attribute("team.steps", len(plan.steps))
            span.set_attribute("team.agents", ",".join(s.agent_id for s in plan.steps))
            return plan

    async def _plan(
        self, question: str, agent_ids: list[str] | None, history: list[Message]
    ) -> Plan:
        if agent_ids:
            unknown = [a for a in agent_ids if a not in self.catalog.agents]
            if unknown:
                raise KeyError(f"Unknown agent_id: {', '.join(unknown)}")
            candidates = [
                Candidate(a, self.catalog.agents[a].name, self.catalog.agents[a].division, 1.0)
                for a in dict.fromkeys(agent_ids)
            ]
            max_steps = max(len(candidates), self.settings.team_max_agents)
        else:
            candidates = await self.router.retrieve(question, self.settings.team_candidates_k)
            max_steps = self.settings.team_max_agents

        if not candidates:
            decision = await self.router.route(question)
            return Plan(
                [PlanStep("s1", decision.agent_id, decision.agent_name, question)],
                method="retrieval",
                reasoning="no candidates; single default specialist",
            )

        planned = await self._llm_plan(question, candidates, max_steps, history)
        if planned is not None:
            steps, reasoning, usage = planned
            return Plan(
                steps,
                method="override" if agent_ids else "llm",
                reasoning=reasoning,
                candidates=candidates,
                usage=usage,
            )
        return self._fallback(question, candidates, forced=bool(agent_ids))

    def _fallback(self, question: str, candidates: list[Candidate], *, forced: bool) -> Plan:
        """Without a usable LLM plan: every pinned agent answers the whole request, or the
        best retrieval hits from distinct divisions do, for a multi-perspective answer."""
        if forced:
            picked = candidates
        else:
            picked, divisions = [], set()
            for c in candidates:
                if c.score > 0 and c.division not in divisions:
                    picked.append(c)
                    divisions.add(c.division)
                if len(picked) >= min(3, self.settings.team_max_agents):
                    break
            picked = picked or candidates[:1]
        steps = [PlanStep(f"s{i}", c.agent_id, c.name, question) for i, c in enumerate(picked, 1)]
        return Plan(
            steps,
            method="override" if forced else "retrieval",
            reasoning="planner unavailable; parallel specialists on the full request",
            candidates=candidates,
        )

    async def _llm_plan(
        self,
        question: str,
        candidates: list[Candidate],
        max_steps: int,
        history: list[Message],
    ) -> tuple[list[PlanStep], str, dict[str, Any]] | None:
        listing = "\n".join(
            f"- id: {c.agent_id}\n  name: {c.name}\n  "
            f"does: {self.catalog.agents[c.agent_id].description[:200]}"
            for c in candidates
        )
        recent = "\n".join(f"{m['role']}: {m['content'][:500]}" for m in history[-4:])
        prompt = f"Candidates:\n{listing}\n\n"
        if recent:
            prompt += f"Conversation so far:\n{recent}\n\n"
        prompt += f"Request:\n{question}"
        messages = [
            {"role": "system", "content": PLANNER_PROMPT.format(max_steps=max_steps)},
            {"role": "user", "content": prompt},
        ]
        try:
            result = await self.llm.complete(
                messages, model=self.settings.planner_model, temperature=0.0, max_tokens=1200
            )
        except Exception:
            log.exception("planner LLM call failed; falling back to retrieval plan")
            return None
        data = _parse_json(result.text) or {}
        steps = validate_steps(data, self.catalog, {c.agent_id for c in candidates}, max_steps)
        if not steps:
            log.warning("planner returned no valid steps; falling back")
            return None
        return steps, str(data.get("reasoning", ""))[:300], result.usage()


def ready_steps(steps: list[dict[str, Any]], done: set[str]) -> list[dict[str, Any]]:
    """Steps not yet run whose dependencies have all finished (failed ones count as done,
    so a failure degrades the answer instead of stalling the team)."""
    return [s for s in steps if s["id"] not in done and all(d in done for d in s["depends_on"])]


def worker_messages(
    agent_prompt: str,
    question: str,
    task: str,
    context: list[dict[str, Any]],
    max_chars: int,
) -> list[Message]:
    parts = [
        f"You are working as part of a team of specialists on this request:\n{question}",
        f"Your part:\n{task}",
    ]
    for c in context:
        body = c["output"][:max_chars] if c.get("output") else f"(failed: {c.get('error')})"
        parts.append(f"Input from {c['agent_name']}:\n{body}")
    parts.append("Answer only your part, concretely.")
    return [
        {"role": "system", "content": agent_prompt},
        {"role": "user", "content": "\n\n".join(parts)},
    ]


def synthesis_messages(
    question: str, results: list[dict[str, Any]], history: list[Message], max_chars: int
) -> list[Message]:
    blocks = [
        f"### {r['agent_name']} ({r['task'][:200]})\n"
        + (r["output"][:max_chars] if r.get("output") else f"(failed: {r.get('error')})")
        for r in results
    ]
    return [
        {"role": "system", "content": SYNTHESIZER_PROMPT},
        *history,
        {
            "role": "user",
            "content": f"Request:\n{question}\n\nContributions:\n\n" + "\n\n".join(blocks),
        },
    ]


def fallback_synthesis(results: list[dict[str, Any]]) -> str:
    return (
        "\n\n".join(f"## {r['agent_name']}\n\n{r['output']}" for r in results if r.get("output"))
        or "The specialist team could not produce an answer."
    )
