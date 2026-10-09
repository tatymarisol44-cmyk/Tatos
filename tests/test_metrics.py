"""SLO signals (docs/slo.md): the request histogram is labelled by route TEMPLATE in
second-scale buckets, model calls and circuit openings are counted, and the backlog gauges
reflect the database."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from opentelemetry import metrics
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from orchestrator.api.app import create_app
from orchestrator.catalog import Catalog
from orchestrator.config import Settings
from orchestrator.llm import FakeLLM, GuardedLLM, LLMResult, Message
from orchestrator.resilience import CircuitBreaker
from orchestrator.service import Orchestrator

ADMIN = {"X-API-Key": "test-key"}
_READER: InMemoryMetricReader | None = None


@pytest.fixture(scope="module")
def reader() -> InMemoryMetricReader:
    """The global meter provider can be set once per process: the first test module to
    need metrics installs an in-memory reader, the instruments (created at import as
    proxies) bind to it."""
    global _READER
    if _READER is None:
        if isinstance(metrics.get_meter_provider(), MeterProvider):
            pytest.skip("another meter provider is already installed")
        _READER = InMemoryMetricReader()
        metrics.set_meter_provider(MeterProvider(metric_readers=[_READER]))
    return _READER


def points(reader: InMemoryMetricReader, name: str) -> list[Any]:
    data = reader.get_metrics_data()
    found: list[Any] = []
    for rm in data.resource_metrics if data else []:
        for sm in rm.scope_metrics:
            for metric in sm.metrics:
                if metric.name == name:
                    found.extend(metric.data.data_points)
    return found


@pytest.fixture
def client(
    settings: Settings, catalog: Catalog, reader: InMemoryMetricReader
) -> Iterator[tuple[TestClient, Orchestrator]]:
    settings.tenant_packs = {"acme": "dental"}
    orch = Orchestrator(settings, catalog=catalog, llm=FakeLLM())
    with TestClient(create_app(settings, orch)) as c:
        yield c, orch


def test_requests_are_labelled_by_template_never_by_id(
    client: tuple[TestClient, Orchestrator], reader: InMemoryMetricReader
) -> None:
    c, _ = client
    c.get("/v1/crm/patients/p-secret-42", headers=ADMIN)
    c.get("/does-not-exist")
    seen = points(reader, "agency.http.server.duration")
    labels = [dict(p.attributes) for p in seen]
    assert {
        "route": "/v1/crm/patients/{patient_id}",
        "method": "GET",
        "status_class": "4xx",
    } in labels
    assert any(lbl["route"] == "unmatched" for lbl in labels)
    assert not any("p-secret-42" in str(lbl) for lbl in labels)
    assert 0.3 in seen[0].explicit_bounds  # second-scale buckets: the SLO threshold exists


class FlakyLLM(FakeLLM):
    def __init__(self) -> None:
        super().__init__()
        self.down = True

    async def _complete(self, messages: list[Message], *, model: str) -> LLMResult:
        if self.down:
            raise ConnectionError("down")
        return await super()._complete(messages, model=model)


async def test_model_calls_and_circuit_openings_are_counted(
    reader: InMemoryMetricReader,
) -> None:
    inner = FlakyLLM()
    llm = GuardedLLM(inner, CircuitBreaker("llm-metrics", failures=2, cooldown_s=30))

    def total(name: str, **attrs: str) -> float:
        return sum(
            p.value
            for p in points(reader, name)
            if all(p.attributes.get(k) == v for k, v in attrs.items())
        )

    before = {o: total("agency.llm.calls", outcome=o) for o in ("error", "circuit_open", "ok")}
    opened = total("agency.circuit.opened", dependency="llm-metrics")
    for _ in range(3):
        with pytest.raises(Exception):  # noqa: B017
            await llm.complete([{"role": "user", "content": "x"}], model="m")
    assert total("agency.llm.calls", outcome="error") - before["error"] == 2
    assert total("agency.llm.calls", outcome="circuit_open") - before["circuit_open"] == 1
    assert total("agency.circuit.opened", dependency="llm-metrics") - opened == 1
    llm.breaker = CircuitBreaker("llm-metrics", failures=2, cooldown_s=30)
    inner.down = False
    await llm.complete([{"role": "user", "content": "hola"}], model="m")
    assert total("agency.llm.calls", outcome="ok") - before["ok"] == 1
    assert points(reader, "agency.llm.duration")


def test_backlog_gauges_follow_the_database(
    client: tuple[TestClient, Orchestrator], reader: InMemoryMetricReader
) -> None:
    c, orch = client
    held = c.post(
        "/v1/chat",
        json={"question": "¿Qué dosis de ibuprofeno tomo?", "subject_id": "p-1"},
        headers=ADMIN,
    ).json()
    assert held["status"] == "pending_review"
    ops = asyncio.run(orch.refresh_ops_gauges())
    assert ops == {
        "reviews_pending": 1.0,
        "alert_oldest_open_age_seconds": 0.0,
        "outbox_pending": 0.0,
        "privacy_steps_due_soon": 0.0,
    }
    [pending] = points(reader, "agency.reviews.pending")
    assert pending.value == 1.0
    assert [p.value for p in points(reader, "agency.alerts.oldest_open_age")] == [0.0]
