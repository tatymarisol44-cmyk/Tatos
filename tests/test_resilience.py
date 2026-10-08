"""Resilience (audit 2026-10-08): the circuit breaker, and chaos tests showing that a
model, vector-store or database outage degrades the product instead of taking it down."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from orchestrator.api.app import create_app
from orchestrator.catalog import Catalog
from orchestrator.config import Settings
from orchestrator.db import session_options, utcnow
from orchestrator.llm import FakeLLM, GuardedLLM, LLMResult, LLMUnavailable, Message
from orchestrator.resilience import CircuitBreaker, CircuitOpen
from orchestrator.service import Orchestrator

ADMIN = {"X-API-Key": "test-key"}


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


async def _boom() -> int:
    raise ConnectionError("provider down")


async def _ok() -> int:
    return 7


# --- the breaker -----------------------------------------------------------------------


async def test_opens_after_consecutive_failures_and_fails_fast() -> None:
    clock = Clock()
    breaker = CircuitBreaker("dep", failures=3, cooldown_s=30, clock=clock)
    for _ in range(3):
        with pytest.raises(ConnectionError):
            await breaker.call(_boom)
    assert breaker.state == "open"
    calls = 0

    async def counted() -> int:
        nonlocal calls
        calls += 1
        return 1

    with pytest.raises(CircuitOpen) as info:
        await breaker.call(counted)
    assert calls == 0 and info.value.retry_after == pytest.approx(30)


async def test_a_success_resets_the_count() -> None:
    breaker = CircuitBreaker("dep", failures=2, cooldown_s=30, clock=Clock())
    with pytest.raises(ConnectionError):
        await breaker.call(_boom)
    assert await breaker.call(_ok) == 7
    with pytest.raises(ConnectionError):
        await breaker.call(_boom)
    assert breaker.state == "closed"  # never two failures in a row


async def test_half_open_lets_one_trial_through() -> None:
    clock = Clock()
    breaker = CircuitBreaker("dep", failures=1, cooldown_s=30, clock=clock)
    with pytest.raises(ConnectionError):
        await breaker.call(_boom)
    clock.now += 31
    assert breaker.state == "half_open"
    with pytest.raises(ConnectionError):  # the trial fails: open again, full cooldown
        await breaker.call(_boom)
    assert breaker.state == "open"
    clock.now += 31
    assert await breaker.call(_ok) == 7  # the trial succeeds: closed
    assert breaker.state == "closed"


async def test_only_one_trial_at_a_time_and_cancellation_frees_it() -> None:
    clock = Clock()
    breaker = CircuitBreaker("dep", failures=1, cooldown_s=30, clock=clock)
    with pytest.raises(ConnectionError):
        await breaker.call(_boom)
    clock.now += 31
    started = asyncio.Event()

    async def slow() -> int:
        started.set()
        await asyncio.sleep(10)
        return 1

    trial = asyncio.create_task(breaker.call(slow))
    await started.wait()
    with pytest.raises(CircuitOpen):  # a second caller does not pile onto the trial
        await breaker.call(_ok)
    trial.cancel()
    with pytest.raises(asyncio.CancelledError):
        await trial
    assert await breaker.call(_ok) == 7  # the cancelled trial did not wedge the breaker


# --- the model -------------------------------------------------------------------------


class DownLLM(FakeLLM):
    """Every call fails, like a provider outage after LiteLLM's retries and fallbacks."""

    def __init__(self) -> None:
        super().__init__()
        self.attempts = 0

    async def _complete(self, messages: list[Message], *, model: str) -> LLMResult:
        self.attempts += 1
        raise ConnectionError("upstream 503 for prompt: <the patient's words>")


async def test_guarded_llm_hides_the_provider_message_and_stops_calling() -> None:
    inner = DownLLM()
    llm = GuardedLLM(inner, CircuitBreaker("llm", failures=2, cooldown_s=30))
    for _ in range(4):
        with pytest.raises(LLMUnavailable) as info:
            await llm.complete([{"role": "user", "content": "x"}], model="m")
        assert "patient" not in str(info.value)  # the prompt never reaches an error/log
    assert inner.attempts == 2  # then the breaker answered by itself


@pytest.fixture
def outage(settings: Settings, catalog: Catalog) -> Iterator[tuple[TestClient, DownLLM]]:
    settings.tenant_packs = {"acme": "dental"}
    inner = DownLLM()
    llm = GuardedLLM(inner, CircuitBreaker("llm", failures=2, cooldown_s=30))
    orch = Orchestrator(settings, catalog=catalog, llm=llm)  # type: ignore[arg-type]
    with TestClient(create_app(settings, orch)) as c:
        yield c, inner


def test_a_model_outage_only_affects_the_assistant(
    outage: tuple[TestClient, DownLLM],
) -> None:
    client, inner = outage
    response = client.post("/v1/chat", json={"question": "Hola"}, headers=ADMIN)
    assert response.status_code == 503
    body = response.json()
    assert body["code"] == "llm_unavailable" and "Nothing was lost" in body["detail"]
    assert int(response.headers["Retry-After"]) >= 1

    # Routing falls back to retrieval; the agenda, CRM and records do not need a model.
    assert client.post("/v1/route", json={"question": "docker"}, headers=ADMIN).status_code == 200
    patient = {"id": "p-1", "display_name": "Ana"}
    assert client.post("/v1/crm/patients", json=patient, headers=ADMIN).status_code == 201
    soon = (utcnow() + timedelta(days=1)).isoformat()
    appt = {"patient_id": "p-1", "starts_at": soon}
    assert client.post("/v1/crm/appointments", json=appt, headers=ADMIN).status_code == 201
    assert len(client.get("/v1/crm/appointments", headers=ADMIN).json()) == 1
    assert client.get("/v1/insights/summary", headers=ADMIN).status_code == 200
    assert client.get("/readyz").status_code == 200  # the pod stays in rotation

    # Once open, the circuit answers without calling the provider at all.
    before = inner.attempts
    for _ in range(3):
        assert client.post("/v1/chat", json={"question": "Hola"}, headers=ADMIN).status_code == 503
    assert inner.attempts == before


# --- the vector store and the database -------------------------------------------------


@pytest.fixture
def client(settings: Settings, catalog: Catalog) -> Iterator[tuple[TestClient, Orchestrator]]:
    orch = Orchestrator(settings, catalog=catalog, llm=FakeLLM())
    with TestClient(create_app(settings, orch)) as c:
        yield c, orch


def test_a_vector_store_outage_answers_without_company_context(
    client: tuple[TestClient, Orchestrator], monkeypatch: pytest.MonkeyPatch
) -> None:
    c, orch = client

    async def down(*args: Any, **kwargs: Any) -> Any:
        raise ConnectionError("qdrant unreachable")

    monkeypatch.setattr(orch.knowledge, "search", down)
    response = c.post("/v1/chat", json={"question": "¿Horario de atención?"}, headers=ADMIN)
    assert response.status_code == 200 and response.json()["answer"]


def test_a_database_outage_takes_the_pod_out_of_rotation(
    client: tuple[TestClient, Orchestrator],
) -> None:
    c, orch = client
    assert c.get("/readyz").status_code == 200

    class DeadEngine:
        def connect(self) -> Any:
            raise ConnectionRefusedError("postgres down")

    real = orch.db.engine
    orch.db.engine = DeadEngine()  # type: ignore[assignment]
    try:
        assert c.get("/readyz").status_code == 503
        assert c.get("/healthz").status_code == 200  # liveness: no restart for a remote outage
    finally:
        orch.db.engine = real
    assert c.get("/readyz").status_code == 200


def test_postgres_sessions_carry_server_side_limits(settings: Settings) -> None:
    assert session_options(settings) == (
        "-c statement_timeout=15000 -c lock_timeout=5000 "
        "-c idle_in_transaction_session_timeout=60000"
    )
