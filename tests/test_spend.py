"""Monthly model spend per tenant and its cap (threat T14)."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from orchestrator.api.app import create_app
from orchestrator.catalog import Catalog
from orchestrator.config import Settings
from orchestrator.llm import FakeLLM
from orchestrator.service import Orchestrator
from orchestrator.spend import BudgetExceeded

ADMIN = {"X-API-Key": "test-key"}
GLOBEX = {"X-API-Key": "other-key"}


@pytest.fixture
def app(settings: Settings, catalog: Catalog) -> Iterator[tuple[TestClient, Orchestrator]]:
    settings.tenant_monthly_budget_usd = 5.0
    orch = Orchestrator(settings, catalog=catalog, llm=FakeLLM())
    with TestClient(create_app(settings, orch)) as c:
        yield c, orch


def test_spend_adds_up_per_tenant_and_month(app: tuple[TestClient, Orchestrator]) -> None:
    client, orch = app
    assert client.post("/v1/chat", json={"question": "hola"}, headers=ADMIN).status_code == 200
    month = client.get("/v1/usage", headers=ADMIN).json()
    assert month["llm_calls"] >= 1 and month["budget_usd"] == 5.0
    asyncio.run(orch.spend.charge("acme", {"cost_usd": 1.25, "llm_calls": 2}))
    asyncio.run(orch.spend.charge("acme", {"cost_usd": 0.75, "llm_calls": 1, "unpriced_calls": 1}))
    month = client.get("/v1/usage", headers=ADMIN).json()
    assert month["cost_usd"] == 2.0 and month["unpriced_calls"] == 1 and month["used_ratio"] == 0.4
    assert client.get("/v1/usage", headers=GLOBEX).json()["cost_usd"] == 0.0  # per tenant


def test_over_the_cap_only_the_assistant_stops(app: tuple[TestClient, Orchestrator]) -> None:
    client, orch = app
    asyncio.run(orch.spend.charge("acme", {"cost_usd": 5.0, "llm_calls": 1}))
    with pytest.raises(BudgetExceeded):
        asyncio.run(orch.spend.check("acme"))
    refused = client.post("/v1/chat", json={"question": "hola"}, headers=ADMIN)
    assert refused.status_code == 429 and refused.json()["code"] == "budget"
    stream = client.post("/v1/chat/stream", json={"question": "hola"}, headers=ADMIN)
    assert stream.status_code == 429
    # Everything else keeps working, and other tenants are not affected.
    assert (
        client.post(
            "/v1/crm/patients", json={"id": "p-1", "display_name": "Ana"}, headers=ADMIN
        ).status_code
        == 201
    )
    assert client.post("/v1/chat", json={"question": "hola"}, headers=GLOBEX).status_code == 200


def test_whatsapp_falls_back_to_fixed_texts_over_the_cap(
    app: tuple[TestClient, Orchestrator], monkeypatch: pytest.MonkeyPatch
) -> None:
    from pydantic import SecretStr

    from tests.test_inbound import SECRET, connect, payload, post, sent_texts

    client, orch = app
    orch.settings.meta_app_secret = SecretStr(SECRET)
    orch.settings.whatsapp_verify_token = SecretStr("verify")
    connect(client)
    seen = sent_texts(orch, monkeypatch)
    asyncio.run(orch.spend.charge("acme", {"cost_usd": 9.0, "llm_calls": 1}))
    before = len(orch.llm.calls)  # type: ignore[attr-defined]
    post(client, payload("hola, ¿atienden sábados?", "wamid.b1"))
    assert "Gracias por escribir a" in seen[-1]["text"]["body"]  # the fixed welcome
    assert len(orch.llm.calls) == before  # type: ignore[attr-defined]
