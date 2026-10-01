"""The governance/CRM SQL on a real Postgres (CI sets TEST_POSTGRES_URL). The rest of the
suite runs the same SQL on SQLite; this catches dialect differences (aggregates over
timestamps, JSON columns, timezone handling)."""

from __future__ import annotations

import os
import uuid
from datetime import timedelta

import httpx
import pytest
from pydantic import SecretStr

from orchestrator.catalog import Catalog
from orchestrator.config import Settings
from orchestrator.db import utcnow
from orchestrator.governance import Purpose
from orchestrator.llm import FakeLLM
from orchestrator.service import Orchestrator

POSTGRES_URL = os.environ.get("TEST_POSTGRES_URL")

pytestmark = pytest.mark.skipif(POSTGRES_URL is None, reason="set TEST_POSTGRES_URL to run")


async def test_full_business_flow_on_postgres(settings: Settings, catalog: Catalog) -> None:
    assert POSTGRES_URL is not None
    settings.database_url = SecretStr(
        POSTGRES_URL.replace("postgresql://", "postgresql+psycopg://", 1)
    )
    tenant = f"pg-{uuid.uuid4().hex[:8]}"  # the database outlives the test run
    settings.tenant_packs = {tenant: "dental"}
    settings.campaign_default_holdout_pct = 0
    orch = Orchestrator(settings, catalog=catalog, llm=FakeLLM())
    await orch.start()
    try:
        now = utcnow()
        await orch.crm.create_patient(
            tenant, {"display_name": "Ana Pérez", "telegram_chat_id": "5"}, "r", patient_id="p1"
        )
        appt = await orch.crm.create_appointment(
            tenant,
            "p1",
            starts_at=now - timedelta(days=400),
            duration_min=30,
            kind="x",
            price=50,
            actor="r",
        )
        await orch.crm.set_appointment_status(tenant, appt["id"], "completed", "r")
        [alert] = await orch.crm.alerts(tenant, now)
        assert alert.kind == "recall_due" and alert.level == "red"

        summary = await orch.insights.summary(tenant, now)
        assert summary["segments"]["dormant"] == 1
        assert summary["high_value"]["patients"] == ["p1"]

        await orch.consents.record(tenant, "p1", Purpose.MARKETING, True, source="f", actor="r")
        orch.settings.telegram_bot_token = SecretStr("123:test-token")
        orch.campaigns.telegram._http = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"ok": True}))
        )
        campaign = await orch.campaigns.create(
            tenant,
            name="c",
            kind="reactivation",
            segment="dormant",
            channel="telegram",
            template="Hola {first_name}, te esperamos. Responde STOP para salir.",
            actor="r",
        )
        await orch.campaigns.approve(tenant, campaign["id"], "owner")
        sent = await orch.campaigns.send(tenant, campaign["id"], "r")
        assert sent["outcomes"] == {"sent": 1}
        results = await orch.campaigns.results(tenant, campaign["id"], now=now + timedelta(days=31))
        assert results["status"] == "final" and results["itt"]["arms"]["treatment"]["n"] == 1

        paused = await orch.chat("hello", tenant=tenant, thread_id="t", force_review=True)
        assert paused.status == "pending_review"
        assert await orch.reviews.count_pending(tenant) == 1
        done = await orch.resolve_review(tenant, "t", approved=True, reviewer="dr")
        assert done.status == "completed"

        erased = await orch.erase_subject(tenant, "p1", actor="dpo")
        assert erased["crm"]["patient"] == "restricted"
        events = await orch.audit.list(tenant, subject_id="p1")
        assert events[0].action == "subject.erased" and events[0].ts.tzinfo is not None
    finally:
        await orch.close()
