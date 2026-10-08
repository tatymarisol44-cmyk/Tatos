"""The demo script must always run: CI drives the same `agency demo` steps against the app
in memory, so a Codespace demo cannot break silently."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from orchestrator.api.app import create_app
from orchestrator.catalog import Catalog
from orchestrator.config import Settings
from orchestrator.demo import Demo
from orchestrator.llm import FakeLLM
from orchestrator.service import Orchestrator


@pytest.fixture
def client(settings: Settings, catalog: Catalog, tmp_path: object) -> Iterator[TestClient]:
    settings.tenant_packs = {"acme": "ec-psychologist"}
    settings.campaign_default_holdout_pct = 20
    settings.meta_app_secret = SecretStr("demo-secret")
    settings.whatsapp_verify_token = SecretStr("demo-verify")
    settings.media_dir = tmp_path / "media"  # type: ignore[operator]
    orch = Orchestrator(settings, catalog=catalog, llm=FakeLLM())
    with TestClient(create_app(settings, orch)) as c:
        yield c


def test_the_whole_demo_runs_end_to_end(client: TestClient) -> None:
    out = Demo(client, "test-key", meta_app_secret="demo-secret").run()
    steps = {s["step"]: s for s in out["report"]}
    assert list(steps) == [
        "team",
        "patients",
        "agenda",
        "clinical record",
        "knowledge base",
        "assistant",
        "patient app (API)",
        "engagement campaign",
        "social media",
        "crisis alert",
        "follow-up",
        "privacy",
    ]
    assert set(out["keys"]) >= {"dra.vera", "recepcion", "marketing", "direccion", "paciente"}
    assert steps["clinical record"]["phq9_alerts"]  # item 9 > 0 reaches a person
    assert steps["clinical record"]["custom_test_alerts"]  # the psychologist's own rule
    assert steps["assistant"]["clinical_question"] == "pending_review"
    assert steps["assistant"]["after_review"] == "professional_edited"
    assert steps["assistant"]["everyday"] == "ai_unreviewed"
    assert steps["engagement campaign"]["mode"] == "simulation"
    assert steps["engagement campaign"]["eligible"] >= 1
    assert set(steps["social media"]["accounts"]) == {
        "instagram",
        "facebook",
        "tiktok",
        "telegram",
        "whatsapp",
    }
    # Without credentials nothing is sent, and the report says so (never "published").
    publications = steps["social media"]["publications"]
    assert publications["instagram"].startswith("dry run")
    assert publications["facebook"].startswith("dry run")  # photo rules verified (FB-PHOTOS)
    assert steps["crisis alert"]["open_alerts"] == 1
    assert steps["agenda"]["double_booking"] == "refused (409)"
    assert steps["follow-up"]["needing_attention"] >= 1  # PHQ-9 item 9 alert at least
    assert steps["privacy"]["audit_chain"]["ok"] is True
    assert "clinical_record" in steps["privacy"]["erasure_retained"]


def test_live_mode_writes_only_to_the_owners_own_chat(client: TestClient) -> None:
    """With a real bot, invented chat ids could reach strangers: only the given chat id
    is stored, every other synthetic patient has no Telegram id at all."""
    demo = Demo(client, "test-key", telegram_chat_id="123456789")
    demo.team()
    ids = demo.patients()
    service = {"X-API-Key": "test-key"}
    chats = [
        client.get(f"/v1/crm/patients/{pid}", headers=service).json()["telegram_chat_id"]
        for pid in ids
    ]
    assert chats[0] == "123456789"
    assert all(c is None for c in chats[1:])


def test_with_a_real_whatsapp_number_the_demo_never_writes_to_an_invented_one(
    client: TestClient,
) -> None:
    """Live WhatsApp: the warm reply would reach whoever owns the invented sender, so the
    crisis is not rehearsed; the owner writes from their own phone instead."""
    out = Demo(
        client,
        "test-key",
        meta_app_secret="demo-secret",
        accounts={"whatsapp": "123456789012345", "facebook": "page-1"},
    ).run()
    steps = {s["step"]: s for s in out["report"]}
    assert "live" in steps["crisis alert"] and "open_alerts" not in steps["crisis alert"]
    accounts = client.get("/v1/social/accounts", headers={"X-API-Key": "test-key"}).json()
    ids = {a["network"]: a["external_id"] for a in accounts}
    assert ids["whatsapp"] == "123456789012345" and ids["facebook"] == "page-1"
