"""Concurrency on a real Postgres, across two "replicas" (second audit).

Two Orchestrator instances share one database, each with its own connection pool, as two
API pods would. Every test fires the competing operations at the same time and checks the
invariant: one winner, no double delivery, no gap in the audit chain. SQLite cannot show
this (one connection, serialised); CI and local runs set TEST_POSTGRES_URL."""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from collections import Counter
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from orchestrator.campaigns import CampaignError, contact_budget
from orchestrator.catalog import Catalog
from orchestrator.config import Settings
from orchestrator.crm import CrmError
from orchestrator.db import utcnow
from orchestrator.governance import Purpose, ThreadBusyError
from orchestrator.llm import FakeLLM
from orchestrator.service import Orchestrator, ReviewNotFoundError

POSTGRES_URL = os.environ.get("TEST_POSTGRES_URL")
pytestmark = pytest.mark.skipif(POSTGRES_URL is None, reason="set TEST_POSTGRES_URL to run")

GOOD = "Hola {first_name}, te esperamos. Responde STOP para salir."


@pytest.fixture
async def replicas(settings: Settings, catalog: Catalog) -> AsyncIterator[tuple[Any, Any, str]]:
    assert POSTGRES_URL is not None
    settings.database_url = SecretStr(
        POSTGRES_URL.replace("postgresql://", "postgresql+psycopg://", 1)
    )
    tenant = f"cc-{uuid.uuid4().hex[:8]}"  # the database outlives the test run
    settings.tenant_packs = {tenant: "dental"}
    settings.campaign_default_holdout_pct = 0
    settings.telegram_bot_token = SecretStr("123:test-token")
    settings.db_auto_migrate = True  # Postgres schemas are versioned (A33)
    pods = [Orchestrator(settings, catalog=catalog, llm=FakeLLM()) for _ in range(2)]
    for pod in pods:
        await pod.start()
    yield pods[0], pods[1], tenant
    for pod in pods:
        await pod.close()


def _telegram(pod: Orchestrator, sent: Counter[str]) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.01)  # keep both replicas in flight together
        sent[json.loads(request.content)["chat_id"]] += 1
        return httpx.Response(200, json={"ok": True})

    pod.campaigns.telegram._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def _patient(pod: Orchestrator, tenant: str, pid: str, chat: str) -> None:
    await pod.crm.create_patient(
        tenant, {"display_name": f"Ana {pid}", "telegram_chat_id": chat}, "r", patient_id=pid
    )
    for purpose in (Purpose.MARKETING, Purpose.ANALYTICS):
        await pod.consents.record(tenant, pid, purpose, True, source="f", actor="r")


async def _approved_campaign(pod: Orchestrator, tenant: str) -> str:
    draft = await pod.campaigns.create(
        tenant,
        name="c",
        kind="education",
        segment="no_visits",
        channel="telegram",
        template=GOOD,
        actor="r",
    )
    await pod.campaigns.approve(tenant, draft["id"], "owner")
    return str(draft["id"])


async def test_monthly_cap_holds_across_replicas(replicas: tuple[Any, Any, str]) -> None:
    a, b, tenant = replicas
    await _patient(a, tenant, "p1", "1")
    now = utcnow()
    results = await asyncio.gather(
        *[(a if i % 2 else b).campaigns._reserve(tenant, "p1", 2, now) for i in range(10)]
    )
    assert sum(results) == 2  # the dental cap, whatever the interleaving
    async with a.db.engine.connect() as conn:
        used = (
            (await conn.execute(contact_budget.select().where(contact_budget.c.tenant == tenant)))
            .mappings()
            .one()["used"]
        )
    assert used == 2


async def test_one_campaign_is_sent_once(replicas: tuple[Any, Any, str]) -> None:
    a, b, tenant = replicas
    sent: Counter[str] = Counter()
    _telegram(a, sent)
    _telegram(b, sent)
    for i in range(20):
        await _patient(a, tenant, f"p{i}", str(1000 + i))
    cid = await _approved_campaign(a, tenant)
    outcomes = await asyncio.gather(
        a.campaigns.send(tenant, cid, "r"),
        b.campaigns.send(tenant, cid, "r"),
        return_exceptions=True,
    )
    assert sum(isinstance(o, CampaignError) for o in outcomes) == 1  # one claim wins
    # Two outbox workers on the two replicas finish anything left, concurrently.
    await asyncio.gather(a.campaigns.run_outbox(), b.campaigns.run_outbox())
    assert len(sent) == 20 and set(sent.values()) == {1}  # everyone once, nobody twice
    final = await a.campaigns.get(tenant, cid)
    assert final["status"] == "completed" and final["recipients"] == {"treatment:sent": 20}


async def test_appointment_transition_race(replicas: tuple[Any, Any, str]) -> None:
    a, b, tenant = replicas
    await _patient(a, tenant, "p1", "1")
    for _ in range(10):  # repeat: interleavings vary
        appt = await a.crm.create_appointment(
            tenant, "p1", starts_at=utcnow(), duration_min=30, kind="x", price=50, actor="r"
        )
        outcomes = await asyncio.gather(
            a.crm.set_appointment_status(tenant, appt["id"], "cancelled", "r"),
            b.crm.set_appointment_status(tenant, appt["id"], "completed", "dr"),
            return_exceptions=True,
        )
        ok = [o for o in outcomes if isinstance(o, dict)]
        assert len(ok) == 1 and sum(isinstance(o, CrmError) for o in outcomes) == 1
        final = (await a.crm.get_appointment(tenant, appt["id"]))["status"]
        assert final == ok[0]["status"]  # the loser changed nothing


async def test_audit_chain_has_no_gaps_under_concurrency(replicas: tuple[Any, Any, str]) -> None:
    a, b, tenant = replicas
    await asyncio.gather(
        *[
            (a if i % 2 else b).audit.record(tenant, "staff", "load.event", f"r/{i}")
            for i in range(60)
        ]
    )
    verdict = await b.audit.verify(tenant)
    assert verdict["ok"] is True and verdict["events"] == 60, verdict


async def test_two_reviewers_one_decision(replicas: tuple[Any, Any, str]) -> None:
    a, b, tenant = replicas
    held = await a.chat("hola", tenant=tenant, thread_id="t1", force_review=True)
    assert held.status == "pending_review"
    outcomes = await asyncio.gather(
        a.resolve_review(tenant, "t1", approved=True, reviewer="dr.a"),
        b.resolve_review(tenant, "t1", approved=False, reviewer="dr.b"),
        return_exceptions=True,
    )
    applied = [o for o in outcomes if not isinstance(o, Exception)]
    assert len(applied) == 1
    assert all(
        isinstance(o, ReviewNotFoundError | ThreadBusyError) for o in outcomes if o not in applied
    )
    review = await a.reviews.get(tenant, "t1")
    assert review is not None and review.status == (
        "approved" if applied[0].status == "completed" else "rejected"
    )


async def test_concurrent_document_replacement_leaves_one_version(
    replicas: tuple[Any, Any, str],
) -> None:
    a, b, tenant = replicas
    await asyncio.gather(
        a.knowledge.add(tenant, "Policy A", "Refunds within 30 days.", doc_id="policy"),
        b.knowledge.add(tenant, "Policy B", "Refunds within 60 days.", doc_id="policy"),
    )
    [doc] = await a.knowledge.documents(tenant)
    assert doc.title in ("Policy A", "Policy B")
    assert await b.knowledge.documents(tenant) == [doc]  # both replicas agree
