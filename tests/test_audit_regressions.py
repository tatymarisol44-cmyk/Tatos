"""Regression tests for the external audit findings closed so far.

Each test is the auditor's reproduction with the assertion inverted: it fails if the
defect comes back. Synthetic data and fake credentials only."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from orchestrator.api.app import create_app
from orchestrator.config import Settings
from orchestrator.llm import FakeLLM
from orchestrator.service import Orchestrator, PendingReviewError, ThreadSubjectError

ROOT = Path(__file__).resolve().parents[1]
SERVICE = {"X-API-Key": "test-key"}


@pytest.fixture
async def o() -> AsyncIterator[Orchestrator]:
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        app_env="test",
        agents_dir=ROOT / "tests/fixtures/agents",
        llm_backend="fake",
        embedding_backend="hashing",
        vector_backend="memory",
        api_keys="test-key:acme,other-key:globex",  # type: ignore[arg-type]
        rate_limit_per_minute=10000,
        tenant_packs={"acme": "dental"},
        campaign_default_holdout_pct=0,
        database_url="sqlite+aiosqlite:///:memory:",  # type: ignore[arg-type]
        checkpointer_backend="memory",
        postgres_url=None,
        remote_agents=[],
        remote_agents_api_key=None,
        telegram_bot_token=None,
        otel_enabled=False,
    )
    obj = Orchestrator(settings, llm=FakeLLM())
    await obj.start()
    yield obj
    await obj.close()


@pytest.fixture
async def c(o: Orchestrator) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(o.settings, o)
    app.state.orchestrator = o  # httpx does not run the lifespan
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
    await app.state.limiter.close()


async def staff(c: httpx.AsyncClient, name: str, *roles: str) -> dict[str, str]:
    resp = await c.post(
        "/v1/admin/staff", json={"name": name, "roles": list(roles)}, headers=SERVICE
    )
    assert resp.status_code == 201, resp.text
    return {"X-API-Key": resp.json()["key"]}


async def test_a02_pending_draft_not_returned_to_non_reviewer(c: httpx.AsyncClient) -> None:
    reception = await staff(c, "maria", "reception")
    resp = await c.post(
        "/v1/chat", json={"question": "hola", "force_review": True}, headers=reception
    )
    body = resp.json()
    assert body["status"] == "pending_review" and body["answer"] is None
    assert "draft_answer" not in body["review"]


async def test_a01_x_actor_cannot_impersonate_a_reviewer(c: httpx.AsyncClient) -> None:
    doctor = await staff(c, "dr.lopez", "reviewer")
    paused = (
        await c.post("/v1/chat", json={"question": "hola", "force_review": True}, headers=doctor)
    ).json()
    resp = await c.post(
        f"/v1/reviews/{paused['thread_id']}",
        headers={**doctor, "X-Actor": "invented.owner"},
        json={"approved": True},
    )
    assert resp.status_code == 200
    assert resp.json()["decision_record"]["review"]["reviewer"] == "dr.lopez"


async def test_a01_owner_approval_comes_from_the_role(c: httpx.AsyncClient) -> None:
    marketing = await staff(c, "mkt", "marketing")
    draft = (
        await c.post(
            "/v1/campaigns",
            json={
                "name": "Discount",
                "kind": "education",
                "segment": "no_visits",
                "template": "Oferta 25%. Responde STOP.",
            },
            headers=marketing,
        )
    ).json()
    assert draft["compliance"]["needs_owner_approval"]
    # Old clients that still send the flag gain nothing from it.
    resp = await c.post(
        f"/v1/campaigns/{draft['id']}/approve", json={"owner_approval": True}, headers=marketing
    )
    assert resp.status_code != 200
    owner = await staff(c, "owner", "owner")
    resp = await c.post(f"/v1/campaigns/{draft['id']}/approve", headers=owner)
    assert resp.status_code == 200 and resp.json()["approved_by"] == "owner"


async def test_a04_stream_does_not_leak_raw_email(o: Orchestrator, c: httpx.AsyncClient) -> None:
    assert isinstance(o.llm, FakeLLM)
    o.llm.agent_replies = ["Contacta a prueba@example.com"]
    aid = next(iter(o.catalog.agents))
    for headers in (await staff(c, "maria", "reception"), SERVICE):
        resp = await c.post(
            "/v1/chat/stream",
            json={"question": "hola", "mode": "team", "agent_ids": [aid], "force_review": True},
            headers=headers,
        )
        assert resp.status_code == 200 and "event: step" in resp.text
        assert "prueba@example.com" not in resp.text


async def test_a04_raw_input_is_not_checkpointed(o: Orchestrator) -> None:
    r = await o.chat("Mi correo es prueba@example.com", tenant="acme", subject_id="p1")
    snap = await o.graph.aget_state(o._config("acme", r.thread_id))
    assert "prueba@example.com" not in snap.values["question"]
    assert "pii:email" in r.guardrails["flags"]


async def test_a03_edited_answer_pii_is_minimised(o: Orchestrator) -> None:
    r = await o.chat("hola", tenant="acme", force_review=True)
    result = await o.resolve_review(
        "acme",
        r.thread_id,
        approved=True,
        reviewer="staff",
        edited_answer="Email prueba@example.com",
    )
    assert result.decision_record is not None
    assert "prueba@example.com" not in (result.answer or "")
    assert "prueba@example.com" not in str(result.decision_record)


async def test_a06_retention_does_not_orphan_pending_reviews(o: Orchestrator) -> None:
    r = await o.chat("hola", tenant="acme", force_review=True)
    assert await o.purge_threads(timedelta(seconds=-1)) == 1
    assert await o.reviews.get("acme", r.thread_id) is None
    follow = await o.chat("nuevo", tenant="acme", thread_id=r.thread_id)
    assert follow.status == "completed"


async def test_a08_thread_cannot_change_subject(o: Orchestrator) -> None:
    aid = next(iter(o.catalog.agents))
    await o.chat(
        "MARCADOR_PRIVADO_PACIENTE_A",
        tenant="acme",
        thread_id="shared",
        subject_id="patient-a",
        agent_id=aid,
    )
    with pytest.raises(ThreadSubjectError):
        await o.chat(
            "Consulta del paciente B",
            tenant="acme",
            thread_id="shared",
            subject_id="patient-b",
            agent_id=aid,
        )


async def test_a05_subject_export_is_not_truncated(o: Orchestrator) -> None:
    for _ in range(105):
        await o.audit.record("acme", "staff", "test.event", "test", subject_id="p1")
    result = await o.export_subject("acme", "p1", "staff")
    assert len(result["audit"]) >= 105


async def test_a29_edited_answer_citations_are_revalidated(o: Orchestrator) -> None:
    await o.knowledge.add("acme", "Shipping", "Shipping costs five dollars.", "d1")
    assert isinstance(o.llm, FakeLLM)
    o.llm.agent_replies = ["Shipping costs five dollars [1]"]
    r = await o.chat(
        "Shipping costs", tenant="acme", force_review=True, agent_id=next(iter(o.catalog.agents))
    )
    result = await o.resolve_review(
        "acme", r.thread_id, approved=True, reviewer="staff", edited_answer="Shipping is free [999]"
    )
    assert "[999]" not in (result.answer or "")
    assert "citation:invalid" in result.guardrails["flags"]


async def test_a10_bot_token_never_reaches_logs(
    o: Orchestrator, caplog: pytest.LogCaptureFixture
) -> None:
    token = "123456:FAKE_AUDIT_TOKEN_NOT_A_SECRET"
    o.settings.telegram_bot_token = SecretStr(token)

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True}, request=request)

    o.campaigns.telegram._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with caplog.at_level(logging.INFO, logger="httpx"):
        assert await o.campaigns.telegram.send("1", "prueba") == "sent"
    assert "FAKE_AUDIT_TOKEN" not in caplog.text


async def test_a07_paused_thread_cannot_be_overwritten_without_review_row(
    o: Orchestrator, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = o.reviews.open

    async def fail(*args: object, **kwargs: object) -> None:
        raise RuntimeError("simulated queue write outage")

    monkeypatch.setattr(o.reviews, "open", fail)
    with pytest.raises(RuntimeError):
        await o.chat("first", tenant="acme", thread_id="orphan", force_review=True)
    monkeypatch.setattr(o.reviews, "open", original)
    # The checkpoint is paused at review: a new turn must not run over it.
    with pytest.raises(PendingReviewError):
        await o.chat("second", tenant="acme", thread_id="orphan")
