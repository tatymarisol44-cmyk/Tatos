"""Regression tests for the external audit findings closed so far.

Each test is the auditor's reproduction with the assertion inverted: it fails if the
defect comes back. Synthetic data and fake credentials only."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import delete, select, update

from orchestrator.api.app import create_app
from orchestrator.campaigns import (
    CampaignError,
    DeliveryUncertain,
    contact_budget,
    has_opt_out,
    recipients,
)
from orchestrator.config import Settings
from orchestrator.db import utcnow
from orchestrator.governance import Purpose, ThreadBusyError, audit_events, reviews
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


# --- step 2: privacy and integrity ---------------------------------------------------


async def test_a05_erasure_removes_conversations_and_pending_drafts(o: Orchestrator) -> None:
    r = await o.chat("Dato privado de prueba", tenant="acme", subject_id="p1", force_review=True)
    done = await o.chat("hola", tenant="acme", subject_id="p1")
    exported = await o.export_subject("acme", "p1", "staff")
    assert {c["thread_id"] for c in exported["conversations"]} == {r.thread_id, done.thread_id}
    assert [rv["thread_id"] for rv in exported["reviews"]] == [r.thread_id]
    assert exported["inventory"]["conversations"] == 2

    erased = await o.erase_subject("acme", "p1", "staff")
    assert erased["conversations"] == 2
    assert {x["store"] for x in erased["retained"]} == {"audit_trail"}  # no CRM record here
    for thread in (r.thread_id, done.thread_id):
        assert not await o.checkpointer.exists(o.thread_key("acme", thread))
    assert await o.reviews.get("acme", r.thread_id) is None
    after = await o.export_subject("acme", "p1", "staff")
    assert after["conversations"] == [] and after["reviews"] == []


async def test_a05_erasure_states_what_is_retained_and_why(o: Orchestrator) -> None:
    await o.crm.create_patient("acme", {"display_name": "Ana Prueba"}, "staff", patient_id="p1")
    erased = await o.erase_subject("acme", "p1", "staff")
    retained = {x["store"]: x["basis"] for x in erased["retained"]}
    assert set(retained) == {"audit_trail", "clinical_record"}
    assert all(retained.values())


async def test_a07_one_run_per_thread(o: Orchestrator, monkeypatch: pytest.MonkeyPatch) -> None:
    entered, release = asyncio.Event(), asyncio.Event()
    original = o.graph.ainvoke

    async def slow(*args: object, **kwargs: object) -> object:
        entered.set()
        await release.wait()
        return await original(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(o.graph, "ainvoke", slow)
    first = asyncio.create_task(o.chat("uno", tenant="acme", thread_id="race"))
    await entered.wait()
    with pytest.raises(ThreadBusyError):
        await o.chat("dos", tenant="acme", thread_id="race")
    release.set()
    assert (await first).status == "completed"
    # The lease was released: the thread is usable again.
    monkeypatch.setattr(o.graph, "ainvoke", original)
    assert (await o.chat("tres", tenant="acme", thread_id="race")).status == "completed"


async def test_a07_no_new_turn_while_a_review_is_resuming(
    o: Orchestrator, monkeypatch: pytest.MonkeyPatch
) -> None:
    paused = await o.chat("hola", tenant="acme", thread_id="race-review", force_review=True)
    entered, release = asyncio.Event(), asyncio.Event()
    original = o.graph.ainvoke

    async def delay_resume(*args: object, **kwargs: object) -> object:
        entered.set()
        await release.wait()
        return await original(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(o.graph, "ainvoke", delay_resume)
    task = asyncio.create_task(
        o.resolve_review("acme", paused.thread_id, approved=True, reviewer="staff")
    )
    await entered.wait()
    assert (await o.reviews.get("acme", paused.thread_id)).status == "resolving"  # type: ignore[union-attr]
    with pytest.raises((ThreadBusyError, PendingReviewError)):
        await o.chat("nuevo", tenant="acme", thread_id=paused.thread_id)
    with pytest.raises(PendingReviewError):  # even without the lease, the state says no
        await o._prepare("nuevo", paused.thread_id, None, None, "single", "acme", None, False)
    release.set()
    assert (await task).status == "completed"
    assert (await o.reviews.get("acme", paused.thread_id)).status == "approved"  # type: ignore[union-attr]


async def test_a07_failed_resume_returns_the_review_to_pending(
    o: Orchestrator, monkeypatch: pytest.MonkeyPatch
) -> None:
    paused = await o.chat("hola", tenant="acme", thread_id="fail", force_review=True)

    async def boom(*args: object, **kwargs: object) -> object:
        raise RuntimeError("model outage")

    monkeypatch.setattr(o.graph, "ainvoke", boom)
    with pytest.raises(RuntimeError):
        await o.resolve_review("acme", paused.thread_id, approved=True, reviewer="staff")
    review = await o.reviews.get("acme", paused.thread_id)
    assert review is not None and review.status == "pending" and review.decision is None


async def test_a07_startup_reconciles_a_crashed_resolution(o: Orchestrator) -> None:
    paused = await o.chat("hola", tenant="acme", thread_id="crash", force_review=True)
    assert await o.reviews.claim("acme", paused.thread_id, {"approved": True})
    # Simulate a replica that died after claiming, long enough ago.
    async with o.db.engine.begin() as conn:
        await conn.execute(
            update(reviews)
            .where(reviews.c.thread_id == paused.thread_id)
            .values(resolved_at=utcnow() - timedelta(hours=1))
        )
    assert await o.reconcile_reviews() == 1
    review = await o.reviews.get("acme", paused.thread_id)
    assert review is not None and review.status == "pending"  # the graph is still paused


async def test_a21_audit_failure_rolls_back_the_business_write(
    o: Orchestrator, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fail(*args: object, **kwargs: object) -> None:
        raise RuntimeError("audit store outage")

    monkeypatch.setattr(o.audit, "record_in", fail)
    with pytest.raises(RuntimeError):
        await o.crm.create_patient("acme", {"display_name": "Ana Prueba"}, "staff", patient_id="p1")
    with pytest.raises(RuntimeError):
        await o.consents.record("acme", "p1", Purpose.MARKETING, True, source="t", actor="s")
    monkeypatch.undo()
    assert await o.crm._patient("acme", "p1") is None
    assert await o.consents.get("acme", "p1") == {}
    # A retry succeeds cleanly: no half-written state to conflict with.
    await o.crm.create_patient("acme", {"display_name": "Ana Prueba"}, "staff", patient_id="p1")


async def test_audit_chain_detects_tampering(o: Orchestrator) -> None:
    for i in range(5):
        await o.audit.record("acme", "staff", "test.event", "test", details={"i": i, "x": [1]})
    await o.audit.record("globex", "staff", "test.event", "test")
    assert (await o.audit.verify("acme"))["ok"] is True
    assert (await o.audit.verify("globex"))["events"] == 1

    async with o.db.engine.begin() as conn:  # someone edits event 3 directly in the database
        await conn.execute(
            update(audit_events)
            .where(audit_events.c.tenant == "acme", audit_events.c.seq == 3)
            .values(actor="someone-else")
        )
    assert await o.audit.verify("acme") == {"ok": False, "events": 2, "broken_at": 3}
    assert (await o.audit.verify("globex"))["ok"] is True  # chains are per tenant


async def test_audit_chain_detects_deleted_events(o: Orchestrator) -> None:
    for _ in range(3):
        await o.audit.record("acme", "staff", "test.event", "test")
    async with o.db.engine.begin() as conn:  # the last event disappears
        await conn.execute(
            delete(audit_events).where(audit_events.c.tenant == "acme", audit_events.c.seq == 3)
        )
    assert await o.audit.verify("acme") == {"ok": False, "events": 2, "broken_at": 3}


async def test_audit_chain_survives_erasure_and_is_exposed(
    o: Orchestrator, c: httpx.AsyncClient
) -> None:
    await o.chat("hola", tenant="acme", subject_id="p1")
    await o.erase_subject("acme", "p1", "staff")
    resp = await c.get("/v1/audit/verify", headers=SERVICE)
    assert resp.status_code == 200 and resp.json()["ok"] is True
    reception = await staff(c, "maria", "reception")
    assert (await c.get("/v1/audit/verify", headers=reception)).status_code == 403


# --- step 3: campaigns -----------------------------------------------------------------
GOOD = "Hola {first_name}, te esperamos. Responde STOP para salir."


async def _patient(
    o: Orchestrator, pid: str = "p1", *, consent: bool = True, chat: str | None = "1"
) -> None:
    await o.crm.create_patient(
        "acme", {"display_name": "Ana Prueba", "telegram_chat_id": chat}, "staff", patient_id=pid
    )
    if consent:
        await o.consents.record("acme", pid, Purpose.MARKETING, True, source="t", actor="s")


async def _campaign(o: Orchestrator) -> str:
    draft = await o.campaigns.create(
        "acme",
        name="Test",
        kind="education",
        segment="no_visits",
        channel="telegram",
        template=GOOD,
        actor="staff",
        holdout_pct=0,
    )
    await o.campaigns.approve("acme", draft["id"], "staff")
    return str(draft["id"])


async def _status(o: Orchestrator, cid: str, pid: str = "p1") -> str:
    async with o.db.engine.connect() as conn:
        row = (
            await conn.execute(
                select(recipients.c.status).where(
                    recipients.c.campaign_id == cid, recipients.c.patient_id == pid
                )
            )
        ).first()
    assert row is not None
    return str(row.status)


def test_a11_opt_out_must_be_an_instruction() -> None:
    assert has_opt_out(GOOD)
    assert has_opt_out("Reply STOP to unsubscribe.")
    assert has_opt_out("Si no quieres recibir más mensajes, responde BAJA.")
    for bad in ("Promoción nonstop para clientes.", "Stop by our clinic!", "Pare aquí"):
        assert not has_opt_out(bad), bad


async def test_a11_stop_reply_unsubscribes_end_to_end(
    o: Orchestrator, c: httpx.AsyncClient
) -> None:
    await _patient(o, chat="777")
    o.settings.telegram_webhook_secret = SecretStr("whsec-test-not-a-secret")
    update_ = {"update_id": 1, "message": {"chat": {"id": 777}, "text": " Stop "}}
    url = "/v1/channels/telegram/acme"
    assert (await c.post(url, json=update_)).status_code == 401
    bad = {"X-Telegram-Bot-Api-Secret-Token": "wrong"}
    assert (await c.post(url, json=update_, headers=bad)).status_code == 401
    good = {"X-Telegram-Bot-Api-Secret-Token": "whsec-test-not-a-secret"}
    assert (await c.post(url, json=update_, headers=good)).status_code == 200
    assert not await o.consents.has("acme", "p1", Purpose.MARKETING)
    trail = await o.audit.list("acme", subject_id="p1")
    assert trail[0].action == "consent.withdrawn" and trail[0].actor == "channel:telegram"
    # And nothing is sent to them afterwards.
    cid = await _campaign(o)
    await o.campaigns.send("acme", cid, "staff")
    assert await _status(o, cid) == "skipped_no_consent"


async def test_a11_webhook_is_off_without_a_secret(c: httpx.AsyncClient) -> None:
    resp = await c.post("/v1/channels/telegram/acme", json={"message": {"text": "STOP"}})
    assert resp.status_code == 404


async def test_a12_failure_before_sending_is_recoverable(
    o: Orchestrator, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _patient(o)
    cid = await _campaign(o)

    async def fail(*args: object, **kwargs: object) -> bool:
        raise RuntimeError("simulated db outage")

    monkeypatch.setattr(o.campaigns, "_reserve", fail)
    with pytest.raises(RuntimeError):
        await o.campaigns.send("acme", cid, "staff")
    campaign = await o.campaigns.get("acme", cid)
    assert campaign["status"] == "sending"  # not "sent" while nothing was delivered
    assert await _status(o, cid) == "queued"
    monkeypatch.undo()
    await o.campaigns.run_outbox()  # the worker finishes the job
    assert await _status(o, cid) == "dry_run"
    assert (await o.campaigns.get("acme", cid))["status"] == "completed"


async def test_a12_crash_after_sending_is_never_resent(
    o: Orchestrator, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _patient(o)
    cid = await _campaign(o)
    sent: list[str] = []

    async def deliver(chat: str, text: str) -> str:
        sent.append(chat)
        raise DeliveryUncertain("timeout")

    monkeypatch.setattr(o.campaigns.telegram, "send", deliver)
    result = await o.campaigns.send("acme", cid, "staff")
    assert result["status"] == "partial_failed" and await _status(o, cid) == "uncertain"
    with pytest.raises(CampaignError):  # uncertain is not "failed": no automatic retry
        await o.campaigns.retry_failed("acme", cid, "staff")
    await o.campaigns.run_outbox()
    assert sent == ["1"]
    resolved = await o.campaigns.resolve_uncertain("acme", cid, "p1", True, "staff")
    assert resolved["recipients"] == {"treatment:sent": 1}


async def test_a12_stale_claim_becomes_uncertain(o: Orchestrator) -> None:
    await _patient(o)
    cid = await _campaign(o)
    await o.campaigns.send("acme", cid, "staff")
    async with o.db.engine.begin() as conn:  # a worker died between claim and result
        await conn.execute(
            update(recipients)
            .where(recipients.c.campaign_id == cid)
            .values(status="sending", claimed_at=utcnow() - timedelta(hours=1))
        )
    stats = await o.campaigns.run_outbox()
    assert stats["recovered"] == 1 and await _status(o, cid) == "uncertain"


async def test_a12_definite_failures_can_be_retried(
    o: Orchestrator, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _patient(o)
    cid = await _campaign(o)

    async def refuse(chat: str, text: str) -> str:
        raise RuntimeError("telegram HTTP 400")

    monkeypatch.setattr(o.campaigns.telegram, "send", refuse)
    assert (await o.campaigns.send("acme", cid, "staff"))["status"] == "partial_failed"
    monkeypatch.undo()
    retried = await o.campaigns.retry_failed("acme", cid, "staff")
    assert retried["status"] == "completed" and retried["recipients"] == {"treatment:dry_run": 1}


async def test_a13_late_edit_cannot_reopen_a_sent_campaign(
    o: Orchestrator, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _patient(o)
    draft = await o.campaigns.create(
        "acme",
        name="Race",
        kind="education",
        segment="no_visits",
        channel="telegram",
        template=GOOD,
        actor="staff",
        holdout_pct=0,
    )
    cid = draft["id"]
    read, resume = asyncio.Event(), asyncio.Event()
    original = o.campaigns.get
    first = True

    async def blocked_get(*args: object, **kwargs: object) -> dict[str, object]:
        nonlocal first
        result = await original(*args, **kwargs)  # type: ignore[arg-type]
        if first:
            first = False
            read.set()
            await resume.wait()
        return result

    monkeypatch.setattr(o.campaigns, "get", blocked_get)
    edit = asyncio.create_task(o.campaigns.update_template("acme", cid, GOOD + " Otro.", "s"))
    await read.wait()
    await o.campaigns.approve("acme", cid, "staff")
    await o.campaigns.send("acme", cid, "staff")
    resume.set()
    with pytest.raises(CampaignError):
        await edit
    after = await original("acme", cid)
    assert after["status"] == "completed" and after["template"] == GOOD
    with pytest.raises(CampaignError):
        await o.campaigns.send("acme", cid, "staff")


async def test_a14_concurrent_campaigns_share_the_monthly_cap(
    o: Orchestrator, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _patient(o)  # dental pack: at most 2 messages a month
    await o.campaigns.send("acme", await _campaign(o), "staff")
    c1, c2 = await _campaign(o), await _campaign(o)
    sent: list[str] = []

    async def slow(chat: str, text: str) -> str:
        sent.append(chat)
        await asyncio.sleep(0.05)  # both campaigns are in flight at once
        return "sent"

    monkeypatch.setattr(o.campaigns.telegram, "send", slow)
    await asyncio.gather(
        o.campaigns.send("acme", c1, "staff"), o.campaigns.send("acme", c2, "staff")
    )
    assert len(sent) == 1
    assert sorted([await _status(o, c1), await _status(o, c2)]) == ["sent", "skipped_cap"]
    async with o.db.engine.connect() as conn:
        used = (await conn.execute(select(contact_budget.c.used))).scalar_one()
    assert used == 2


async def test_a15_withdrawal_mid_batch_stops_later_sends(
    o: Orchestrator, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _patient(o, "p1")
    await _patient(o, "p2", chat="2")
    cid = await _campaign(o)
    sent: list[str] = []

    async def deliver(chat: str, text: str) -> str:
        sent.append(chat)
        if len(sent) == 1:
            for pid in ("p1", "p2"):
                await o.consents.record(
                    "acme", pid, Purpose.MARKETING, False, source="STOP", actor="patient"
                )
        return "sent"

    monkeypatch.setattr(o.campaigns.telegram, "send", deliver)
    await o.campaigns.send("acme", cid, "staff")
    assert len(sent) == 1
    assert sorted([await _status(o, cid, "p1"), await _status(o, cid, "p2")]) == [
        "sent",
        "skipped_no_consent",
    ]


async def test_a15_cancel_mid_batch_stops_later_sends(
    o: Orchestrator, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _patient(o, "p1")
    await _patient(o, "p2", chat="2")
    cid = await _campaign(o)
    sent: list[str] = []

    async def deliver(chat: str, text: str) -> str:
        sent.append(chat)
        await o.campaigns.cancel("acme", cid, "owner")
        return "sent"

    monkeypatch.setattr(o.campaigns.telegram, "send", deliver)
    result = await o.campaigns.send("acme", cid, "staff")
    assert len(sent) == 1 and result["status"] == "cancelled"
    assert "treatment:cancelled" in result["recipients"]


async def test_app_first_then_telegram_fallback(o: Orchestrator) -> None:
    await _patient(o, "p1")
    await _patient(o, "p2", chat="2")
    for pid in ("p1", "p2"):
        await o.principals.create_patient_access("acme", pid, "staff", 30)
    cid = await _campaign(o)
    result = await o.campaigns.send("acme", cid, "staff")
    assert result["recipients"] == {"treatment:in_app": 2}
    assert [x["seen"] for x in await o.campaigns.offers_for("acme", "p1")] == [False]
    # p1 opens the app; p2 does not.
    assert await o.campaigns.mark_seen("acme", "p1") == 1
    assert (await o.campaigns.run_outbox())["fallback"] == 0  # not due yet
    async with o.db.engine.begin() as conn:  # 48 hours later
        await conn.execute(
            update(recipients).values(fallback_due_at=utcnow() - timedelta(minutes=1))
        )
    assert (await o.campaigns.run_outbox())["fallback"] == 1
    assert await _status(o, cid, "p1") == "seen"
    assert await _status(o, cid, "p2") == "dry_run"  # the Telegram fallback


async def test_patient_marks_offers_seen_over_http(o: Orchestrator, c: httpx.AsyncClient) -> None:
    await _patient(o)
    created = await c.post("/v1/crm/patients/p1/access", headers=SERVICE)
    key = {"X-API-Key": created.json()["key"]}
    cid = await _campaign(o)
    await o.campaigns.send("acme", cid, "staff")
    resp = await c.post("/v1/me/offers/seen", headers=key)
    assert resp.json() == {"marked": 1} and await _status(o, cid) == "seen"


# --- retention rule: the clinical record is kept, marketing is anonymised ---------------
CLINICAL = "¿Qué dosis de ibuprofeno tomo?"


async def test_erasure_keeps_reviewed_clinical_conversations(o: Orchestrator) -> None:
    await _patient(o)
    held = await o.chat(CLINICAL, tenant="acme", subject_id="p1", thread_id="clin")
    assert held.status == "pending_review"
    await o.resolve_review(
        "acme", "clin", approved=True, reviewer="dr.lopez", edited_answer="Llámenos, por favor."
    )
    small_talk = await o.chat("¿A qué hora abren?", tenant="acme", subject_id="p1")

    erased = await o.erase_subject("acme", "p1", "dpo")
    assert erased["conversations"] == 2 and erased["clinical_conversations_archived"] == 1
    assert "clinical_record" in {x["store"] for x in erased["retained"]}
    # Both conversations are gone from the chat store...
    for thread in ("clin", small_talk.thread_id):
        assert not await o.checkpointer.exists(o.thread_key("acme", thread))
    # ...and the clinical one lives on in the clinical record, as shown to the patient.
    [note] = await o.crm.clinical_notes("acme", "p1")
    assert note["thread_id"] == "clin" and note["reason"] == "erasure_request"
    assert note["messages"][-1] == {"role": "assistant", "content": "Llámenos, por favor."}
    assert note["review"]["reviewer"] == "dr.lopez"
    exported = await o.export_subject("acme", "p1", "dpo")
    assert [n["thread_id"] for n in exported["crm"]["clinical_notes"]] == ["clin"]


async def test_pending_clinical_question_is_kept_without_the_draft(o: Orchestrator) -> None:
    assert isinstance(o.llm, FakeLLM)
    o.llm.agent_replies = ["BORRADOR_IA_SIN_REVISAR"]
    await o.chat(CLINICAL, tenant="acme", subject_id="p1", thread_id="pend")
    await o.erase_subject("acme", "p1", "dpo")
    [note] = await o.crm.clinical_notes("acme", "p1")
    assert note["messages"] == [{"role": "user", "content": CLINICAL, "unanswered": True}]
    assert note["review"]["status"] == "pending"
    assert "BORRADOR_IA_SIN_REVISAR" not in str(note)
    assert await o.reviews.get("acme", "pend") is None


async def test_retention_purge_archives_clinical_conversations(o: Orchestrator) -> None:
    await o.chat(CLINICAL, tenant="acme", subject_id="p1", thread_id="old")
    await o.resolve_review("acme", "old", approved=False, reviewer="dr.lopez")
    await o.chat("hola", tenant="acme", subject_id="p1", thread_id="chit")
    assert await o.purge_threads(timedelta(seconds=-1)) == 2
    assert [n["thread_id"] for n in await o.crm.clinical_notes("acme", "p1")] == ["old"]
    [note] = await o.crm.clinical_notes("acme", "p1")
    assert note["reason"] == "retention" and note["review"]["status"] == "rejected"
