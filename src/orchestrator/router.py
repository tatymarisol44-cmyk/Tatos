"""Two-stage router: vector retrieval narrows ~300 agents to top-k candidates, then a
small LLM picks one. The LLM can only choose among retrieved ids, and any failure
falls back to the best retrieval hit, so routing never fails closed."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from orchestrator.catalog import Catalog
from orchestrator.config import Settings
from orchestrator.embeddings import Embedder
from orchestrator.llm import LLMClient, LLMResult
from orchestrator.telemetry import tracer
from orchestrator.vectorstore import VectorStore

log = logging.getLogger(__name__)

ROUTER_PROMPT = """You are the ROUTER of an AI agency. Pick the single specialist best suited
to answer the user's request. The request may be in any language.
Only choose an id from the candidate list. Reply with JSON only:
{"agent_id": "<id>", "confidence": <0..1>, "reasoning": "<one short sentence>"}"""


@dataclass
class Candidate:
    agent_id: str
    name: str
    division: str
    score: float


@dataclass
class RoutingDecision:
    agent_id: str
    agent_name: str
    confidence: float
    method: Literal["override", "llm", "retrieval", "default"]
    reasoning: str = ""
    candidates: list[Candidate] = field(default_factory=list)
    usage: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _parse_json(text: str) -> dict[str, Any] | None:
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


class Router:
    def __init__(
        self,
        catalog: Catalog,
        embedder: Embedder,
        store: VectorStore,
        llm: LLMClient,
        settings: Settings,
    ) -> None:
        self.catalog = catalog
        self.embedder = embedder
        self.store = store
        self.llm = llm
        self.settings = settings

    @property
    def index_name(self) -> str:
        return f"{self.settings.qdrant_collection}_{self.catalog.version}_{self.embedder.signature}"

    async def build_index(self) -> bool:
        """Embed the catalog if this (catalog, embedder) index doesn't exist. Returns True
        when it (re)indexed."""
        ids = sorted(self.catalog.agents)
        texts = [self.catalog.agents[i].index_text() for i in ids]
        probe = await self.embedder.embed(texts[:1])
        if await self.store.ensure(self.index_name, len(probe[0])):
            return False
        with tracer().start_as_current_span("router.index") as span:
            span.set_attribute("catalog.size", len(ids))
            vectors = await self.embedder.embed(texts)
            await self.store.upsert(ids, vectors)
        log.info("indexed %d agents into %s", len(ids), self.index_name)
        return True

    async def retrieve(self, question: str, k: int | None = None) -> list[Candidate]:
        [vector] = await self.embedder.embed([question])
        hits = await self.store.search(vector, k or self.settings.router_top_k)
        out = []
        for hit in hits:
            agent = self.catalog.get(hit.agent_id)
            if agent:
                out.append(Candidate(agent.id, agent.name, agent.division, round(hit.score, 4)))
        return out

    def _decision(self, agent_id: str, **kwargs: Any) -> RoutingDecision:
        agent = self.catalog.agents[agent_id]
        return RoutingDecision(agent_id=agent_id, agent_name=agent.name, **kwargs)

    async def route(self, question: str, override: str | None = None) -> RoutingDecision:
        with tracer().start_as_current_span("router.route") as span:
            decision = await self._route(question, override)
            span.set_attribute("router.agent_id", decision.agent_id)
            span.set_attribute("router.method", decision.method)
            span.set_attribute("router.confidence", decision.confidence)
            return decision

    async def _route(self, question: str, override: str | None) -> RoutingDecision:
        if override:
            if override not in self.catalog.agents:
                raise KeyError(f"Unknown agent_id: {override}")
            return self._decision(override, confidence=1.0, method="override")

        candidates = await self.retrieve(question)
        if not candidates:
            return self._default(candidates, "no retrieval candidates")
        best = candidates[0]

        if self.settings.router_use_llm and len(candidates) > 1:
            picked = await self._llm_pick(question, candidates)
            if picked is not None:
                return picked

        if best.score <= 0 and self.settings.default_agent_id:
            return self._default(candidates, "no lexical/semantic match")
        return self._decision(
            best.agent_id,
            confidence=max(0.0, min(1.0, best.score)),
            method="retrieval",
            reasoning="top retrieval hit",
            candidates=candidates,
        )

    def _default(self, candidates: list[Candidate], why: str) -> RoutingDecision:
        default = self.settings.default_agent_id
        if not default or default not in self.catalog.agents:
            default = candidates[0].agent_id if candidates else sorted(self.catalog.agents)[0]
        return self._decision(
            default, confidence=0.0, method="default", reasoning=why, candidates=candidates
        )

    async def _llm_pick(self, question: str, candidates: list[Candidate]) -> RoutingDecision | None:
        listing = "\n".join(
            f"- id: {c.agent_id}\n  name: {c.name}\n  "
            f"does: {self.catalog.agents[c.agent_id].description[:300]}"
            for c in candidates
        )
        messages = [
            {"role": "system", "content": ROUTER_PROMPT},
            {"role": "user", "content": f"Candidates:\n{listing}\n\nRequest:\n{question}"},
        ]
        try:
            result: LLMResult = await self.llm.complete(
                messages, model=self.settings.router_model, temperature=0.0, max_tokens=200
            )
        except Exception:
            log.exception("router LLM call failed; falling back to retrieval")
            return None
        data = _parse_json(result.text) or {}
        agent_id = str(data.get("agent_id", ""))
        allowed = {c.agent_id for c in candidates}
        if agent_id not in allowed:
            log.warning("router LLM returned invalid id %r; falling back", agent_id)
            return None
        try:
            confidence = float(data.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        if confidence < self.settings.router_min_confidence:
            return None
        return self._decision(
            agent_id,
            confidence=min(1.0, confidence),
            method="llm",
            reasoning=str(data.get("reasoning", ""))[:300],
            candidates=candidates,
            usage=result.usage(),
        )
