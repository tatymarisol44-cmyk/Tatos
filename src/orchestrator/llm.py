"""Provider-agnostic chat completion. LiteLLM covers Claude, OpenAI, Gemini, Mistral
and Llama (Ollama/vLLM); `FakeLLM` is deterministic for tests and offline dev."""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from orchestrator import usage
from orchestrator.config import Settings
from orchestrator.resilience import CircuitBreaker, CircuitOpen
from orchestrator.telemetry import LLM_CALLS, LLM_DURATION, tracer

Message = dict[str, str]


@dataclass
class LLMResult:
    text: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0

    def usage(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cost_usd": round(self.cost_usd, 6),
        }


class LLMClient(Protocol):
    async def complete(
        self,
        messages: list[Message],
        *,
        model: str,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> LLMResult: ...


class LiteLLMClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def complete(
        self,
        messages: list[Message],
        *,
        model: str,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> LLMResult:
        import litellm

        with tracer().start_as_current_span("gen_ai.chat") as span:
            span.set_attribute("gen_ai.request.model", model)
            resp = await litellm.acompletion(
                model=model,
                messages=messages,
                temperature=self.settings.llm_temperature if temperature is None else temperature,
                max_tokens=max_tokens or self.settings.llm_max_tokens,
                timeout=self.settings.llm_timeout_s,
                num_retries=self.settings.llm_num_retries,
                fallbacks=self.settings.llm_fallback_models or None,
            )
            cost: float | None
            try:
                cost = float(litellm.completion_cost(completion_response=resp))
            except Exception:  # unknown pricing for local/self-hosted models
                cost = None
            tokens = getattr(resp, "usage", None)
            result = LLMResult(
                text=resp.choices[0].message.content or "",
                model=getattr(resp, "model", model) or model,
                input_tokens=getattr(tokens, "prompt_tokens", 0) or 0,
                output_tokens=getattr(tokens, "completion_tokens", 0) or 0,
                cost_usd=cost or 0.0,
            )
            usage.record(
                "llm",
                result.model,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
                cost_usd=cost,
            )
            span.set_attribute("gen_ai.response.model", result.model)
            span.set_attribute("gen_ai.usage.input_tokens", result.input_tokens)
            span.set_attribute("gen_ai.usage.output_tokens", result.output_tokens)
            return result


# What the fake extractor recognises in a sentence that states a preference.
_FAKE_PREFERENCES = [
    (r"\b(tarde|afternoon)\b", "schedule", "afternoon"),
    (r"\b(mañana|morning)\b", "schedule", "morning"),
    (r"\btelegram\b", "channel", "telegram"),
    (r"\b(español|spanish)\b", "language", "es"),
    (r"\b(inglés|english)\b", "language", "en"),
]


@dataclass
class FakeLLM:
    """Router calls get the first candidate id; planner calls get a two-step sequential
    plan over the first two candidates; judge calls get top scores (override with
    `judge_reply`); the memory extractor keeps the customer's sentences that state a
    preference; the copywriter and the insights analyst get a fixed text; agent and
    synthesizer calls get an echo answer (override the first ones with `agent_replies`)."""

    calls: list[list[Message]] = field(default_factory=list)
    router_reply: str | None = None
    planner_reply: str | None = None
    fail_synthesis: bool = False
    judge_reply: str | None = None
    memory_reply: str | None = None
    agent_replies: list[str] = field(default_factory=list)
    # Load tests: wait like a provider would (FAKE_LLM_LATENCY_MS), and do not keep every
    # prompt in memory for a long run.
    latency_s: float = 0.0
    record_calls: bool = True

    async def complete(
        self,
        messages: list[Message],
        *,
        model: str,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> LLMResult:
        if self.latency_s:
            await asyncio.sleep(self.latency_s)
        result = await self._complete(messages, model=model)
        if not self.record_calls:
            self.calls.clear()
        usage.record(
            "llm",
            result.model,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            cost_usd=result.cost_usd,
        )
        return result

    async def _complete(self, messages: list[Message], *, model: str) -> LLMResult:
        self.calls.append(messages)
        system = messages[0]["content"] if messages and messages[0]["role"] == "system" else ""
        question = messages[-1]["content"]
        if "ROUTER" in system:
            if self.router_reply is not None:
                return LLMResult(self.router_reply, model)
            first = re.search(r"^- id: (\S+)", question, re.MULTILINE)
            payload = {
                "agent_id": first.group(1) if first else "",
                "confidence": 0.9,
                "reasoning": "fake: first candidate",
            }
            return LLMResult(json.dumps(payload), model, 10, 10)
        if "PLANNER" in system:
            if self.planner_reply is not None:
                return LLMResult(self.planner_reply, model)
            ids = re.findall(r"^- id: (\S+)", question, re.MULTILINE)[:2]
            steps = [
                {"id": f"s{i}", "agent_id": a, "task": f"fake task {i}", "depends_on": []}
                for i, a in enumerate(ids, 1)
            ]
            if len(steps) == 2:
                steps[1]["depends_on"] = ["s1"]
            plan = {"steps": steps, "reasoning": "fake: first two candidates in sequence"}
            return LLMResult(json.dumps(plan), model, 20, 20)
        if "JUDGE" in system:
            if self.judge_reply is not None:
                return LLMResult(self.judge_reply, model, 50, 30)
            verdict = {
                c: {"score": 5, "reason": "fake"}
                for c in ("relevance", "faithfulness", "completeness")
            }
            return LLMResult(json.dumps(verdict), model, 50, 30)
        if "MEMORY EXTRACTOR" in system:
            if self.memory_reply is not None:
                return LLMResult(self.memory_reply, model, 30, 10)
            said = re.search(r"CUSTOMER: (.*)", question)
            sentences = re.split(r"(?<=[.!?])\s+", said.group(1)) if said else []
            stated = " ".join(
                s for s in sentences if re.search(r"\b(prefer\w*|prefiero)\b", s, re.I)
            )
            found = [
                {"key": key, "value": value}
                for pattern, key, value in _FAKE_PREFERENCES
                if re.search(pattern, stated, re.I)
            ]
            return LLMResult(json.dumps({"preferences": found}), model, 30, 10)
        if "COPYWRITER" in system:
            return LLMResult(
                "Hola {first_name}, te esperamos para tu control. Agenda tu cita cuando "
                "quieras respondiendo a este mensaje.",
                model,
                40,
                25,
            )
        if "INSIGHTS ANALYST" in system:
            return LLMResult(f"[insights] {question[:200]}", model, 60, 30)
        if "SYNTHESIZER" in system:
            if self.fail_synthesis:
                raise RuntimeError("fake synthesizer outage")
            return LLMResult(f"[synthesis] {question}", model, len(question) // 4, 12)
        if self.agent_replies:
            return LLMResult(self.agent_replies.pop(0), model, len(question) // 4, 12)
        title = system.splitlines()[0] if system else "agent"
        return LLMResult(f"[{title}] {question}", model, len(question) // 4, 12)


class LLMUnavailable(RuntimeError):
    """Every configured model failed (after LiteLLM's retries and fallbacks), or the
    circuit is open. The API answers 503 with Retry-After; nothing else depends on it."""

    def __init__(self, message: str, retry_after: float) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class GuardedLLM:
    """Puts a circuit breaker in front of a real provider (resilience.py)."""

    def __init__(self, inner: LLMClient, breaker: CircuitBreaker) -> None:
        self.inner = inner
        self.breaker = breaker

    async def complete(
        self,
        messages: list[Message],
        *,
        model: str,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> LLMResult:
        start = time.perf_counter()
        try:
            result = await self.breaker.call(
                lambda: self.inner.complete(
                    messages, model=model, temperature=temperature, max_tokens=max_tokens
                )
            )
        except CircuitOpen as exc:
            LLM_CALLS.add(1, {"outcome": "circuit_open"})
            raise LLMUnavailable(str(exc), exc.retry_after) from exc
        except Exception as exc:
            LLM_CALLS.add(1, {"outcome": "error"})
            # The provider's message may quote the prompt: keep only the type.
            raise LLMUnavailable(
                f"model call failed ({type(exc).__name__})", self.breaker.cooldown_s
            ) from exc
        LLM_CALLS.add(1, {"outcome": "ok"})
        LLM_DURATION.record(time.perf_counter() - start, {"model": result.model})
        return result


def build_llm(settings: Settings) -> LLMClient:
    if settings.llm_backend == "fake":
        return FakeLLM(latency_s=settings.fake_llm_latency_ms / 1000, record_calls=False)
    breaker = CircuitBreaker(
        "llm", failures=settings.llm_breaker_failures, cooldown_s=settings.llm_breaker_cooldown_s
    )
    return GuardedLLM(LiteLLMClient(settings), breaker)
