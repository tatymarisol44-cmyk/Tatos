"""Day-before appointment reminders: one per visit on any number of replicas, the right
channel, nothing clinical, a WhatsApp STOP respected."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from orchestrator.api.app import create_app
from orchestrator.catalog import Catalog
from orchestrator.config import Settings
from orchestrator.llm import FakeLLM
from orchestrator.service import Orchestrator
from tests.test_inbound import SECRET, connect, payload, post

ADMIN = {"X-API-Key": "test-key"}


@pytest.fixture
def app(settings: Settings, catalog: Catalog) -> Iterator[tuple[TestClient, Orchestrator]]:
    settings.meta_app_secret = SecretStr(SECRET)
    settings.whatsapp_verify_token = SecretStr("verify")
    settings.whatsapp_reminder_template = "recordatorio_cita"
    orch = Orchestrator(settings, catalog=catalog, llm=FakeLLM())
    with TestClient(create_app(settings, orch)) as c:
        yield c, orch


def book(c: TestClient, pid: str, hours_from_now: float, **patient: str) -> None:
    c.post(
        "/v1/crm/patients", json={"id": pid, "display_name": f"Ana {pid}", **patient}, headers=ADMIN
    )
    starts = (datetime.now(UTC) + timedelta(hours=hours_from_now)).isoformat()
    assert (
        c.post(
            "/v1/crm/appointments", json={"patient_id": pid, "starts_at": starts}, headers=ADMIN
        ).status_code
        == 201
    )


def audit_actions(c: TestClient) -> list[str]:
    return [
        e["action"]
        for e in c.get("/v1/audit", headers=ADMIN).json()
        if e["action"].startswith("appointment.reminder")
    ]


def test_reminders_go_once_by_the_right_channel(
    app: tuple[TestClient, Orchestrator], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, orch = app
    sent: list[tuple[str, str]] = []

    async def telegram(chat_id: str, text: str) -> str:
        sent.append((chat_id, text))
        return "sent"

    monkeypatch.setattr(orch.campaigns.telegram, "send", telegram)
    connect(client)  # the practice's WhatsApp account (no credential: dry run)
    book(client, "p-tg", 20, telegram_chat_id="4242")
    book(client, "p-wa", 22, phone="+1 555 000 1111")
    book(client, "p-app", 23)
    book(client, "p-later", 72, telegram_chat_id="99")  # not due yet
    book(client, "p-soon", 0.5, telegram_chat_id="98")  # too late to remind

    assert asyncio.run(orch.reminders.due()) == 3
    [(chat, text)] = sent
    assert chat == "4242" and "Te recordamos tu cita" in text and "a las" in text
    assert sorted(audit_actions(client)) == [
        "appointment.reminder_dry_run",  # WhatsApp template, no credential
        "appointment.reminder_in_app",
        "appointment.reminder_sent",  # Telegram
    ]
    # A second pass (another replica, a minute later) sends nothing again.
    assert asyncio.run(orch.reminders.due()) == 0 and len(sent) == 1


def test_a_whatsapp_stop_is_respected(app: tuple[TestClient, Orchestrator]) -> None:
    client, orch = app
    connect(client)
    post(client, payload("STOP", "wamid.stop-r"))  # from 15550001111
    book(client, "p-stop", 20, phone="+1 555 000 1111")
    asyncio.run(orch.reminders.due())
    assert audit_actions(client) == ["appointment.reminder_opted_out"]
