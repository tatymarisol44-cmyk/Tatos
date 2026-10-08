"""On-call notices and escalation (ADR 0017): level 1 on every channel at once, the next
level after the escalation delay unless someone acknowledges, once on any number of
replicas, and no patient data in a notice. Synthetic numbers and addresses only."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from collections.abc import Iterator
from datetime import timedelta
from typing import Any, ClassVar

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

import orchestrator.oncall as oncall_module
from orchestrator.api.app import create_app
from orchestrator.catalog import Catalog
from orchestrator.config import Settings
from orchestrator.db import utcnow
from orchestrator.llm import FakeLLM
from orchestrator.oncall import notice_text
from orchestrator.service import Orchestrator

ADMIN = {"X-API-Key": "test-key"}
SECRET = "app-secret-synthetic"
NUMBER_ID = "106540352242922"
CALLER = "15550001111"
CRISIS = "Ya no quiero vivir"


class FakeSMTP:
    sent: ClassVar[list[dict[str, Any]]] = []
    fail: ClassVar[bool] = False

    def __init__(self, host: str, port: int, timeout: float) -> None:
        self.host, self.port = host, port
        self.tls = False

    def __enter__(self) -> FakeSMTP:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def starttls(self, context: Any) -> None:
        self.tls = True

    def login(self, user: str, password: str) -> None:
        self.user = user

    def send_message(self, message: Any) -> None:
        if FakeSMTP.fail:
            raise OSError("connection reset")
        FakeSMTP.sent.append(
            {
                "to": message["To"],
                "subject": message["Subject"],
                "body": message.get_content(),
                "tls": self.tls,
            }
        )


@pytest.fixture
def app(
    settings: Settings, catalog: Catalog, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[TestClient, Orchestrator]]:
    settings.whatsapp_verify_token = SecretStr("verify")
    settings.meta_app_secret = SecretStr(SECRET)
    settings.smtp_host = "smtp.example.test"
    settings.smtp_username = "alerts@example.test"
    settings.smtp_password = SecretStr("smtp-synthetic")
    settings.public_base_url = "https://api.consultorio-demo.ec"
    FakeSMTP.sent, FakeSMTP.fail = [], False
    monkeypatch.setattr(oncall_module.smtplib, "SMTP", FakeSMTP)
    orch = Orchestrator(settings, catalog=catalog, llm=FakeLLM())
    with TestClient(create_app(settings, orch)) as c:
        body = {
            "network": "whatsapp",
            "external_id": NUMBER_ID,
            "handle": "+1 555",
            "secret_ref": "WA_DEMO",
        }
        assert c.post("/v1/social/accounts", json=body, headers=ADMIN).status_code == 201
        yield c, orch


def contact(client: TestClient, level: int, **channels: str) -> str:
    body = {"display_name": f"Guardia {level}", "level": level, **channels}
    response = client.post("/v1/admin/on-call", json=body, headers=ADMIN)
    assert response.status_code == 201, response.text
    return str(response.json()["contact_id"])


def message(client: TestClient, text: str, message_id: str = "wamid.1") -> None:
    doc = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "metadata": {"phone_number_id": NUMBER_ID},
                            "messages": [
                                {
                                    "from": CALLER,
                                    "id": message_id,
                                    "type": "text",
                                    "text": {"body": text},
                                }
                            ],
                        },
                    }
                ]
            }
        ],
    }
    body = json.dumps(doc).encode()
    signature = "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    response = client.post(
        "/v1/channels/whatsapp", content=body, headers={"X-Hub-Signature-256": signature}
    )
    assert response.status_code == 200


def alert_id(client: TestClient) -> str:
    return str(client.get("/v1/social/alerts", headers=ADMIN).json()[0]["alert_id"])


def notices(client: TestClient, aid: str) -> list[tuple[int, str, str]]:
    rows = client.get(f"/v1/social/alerts/{aid}/notifications", headers=ADMIN).json()
    return [(r["level"], r["channel"], r["outcome"]) for r in rows]


def test_a_crisis_tells_level_one_at_once_on_every_channel(
    app: tuple[TestClient, Orchestrator],
) -> None:
    client, _ = app
    contact(client, 1, telegram_chat_id="5550001", email="guardia@example.test")
    contact(client, 2, email="respaldo@example.test")
    message(client, CRISIS)
    aid = alert_id(client)
    # Telegram runs dry without a bot token; the e-mail goes out over STARTTLS.
    assert notices(client, aid) == [(1, "telegram", "dry_run"), (1, "email", "sent")]
    (mail,) = FakeSMTP.sent
    assert mail["to"] == "guardia@example.test" and mail["tls"] is True
    assert mail["subject"] == "ALERTA DE CRISIS"
    assert aid in mail["body"] and "https://api.consultorio-demo.ec/" in mail["body"]
    # No patient data: not the message (never stored) and not the caller's number.
    assert "vivir" not in mail["body"] and CALLER not in mail["body"]


def test_nobody_acknowledges_so_level_two_is_told_after_the_delay(
    app: tuple[TestClient, Orchestrator],
) -> None:
    client, orch = app
    contact(client, 1, email="guardia@example.test")
    contact(client, 2, email="respaldo@example.test")
    message(client, CRISIS)
    aid = alert_id(client)
    start = utcnow()
    assert asyncio.run(orch.oncall.escalate_due(start + timedelta(minutes=4))) == 0  # not yet
    assert asyncio.run(orch.oncall.escalate_due(start + timedelta(minutes=5, seconds=5))) == 1
    assert notices(client, aid) == [(1, "email", "sent"), (2, "email", "sent")]
    assert [m["to"] for m in FakeSMTP.sent] == ["guardia@example.test", "respaldo@example.test"]


def test_acknowledging_stops_the_escalation(app: tuple[TestClient, Orchestrator]) -> None:
    client, orch = app
    contact(client, 1, email="guardia@example.test")
    contact(client, 2, email="respaldo@example.test")
    message(client, CRISIS)
    aid = alert_id(client)
    assert client.post(f"/v1/social/alerts/{aid}/ack", headers=ADMIN).status_code == 204
    assert client.post(f"/v1/social/alerts/{aid}/ack", headers=ADMIN).status_code == 404
    assert asyncio.run(orch.oncall.escalate_due(utcnow() + timedelta(hours=1))) == 0
    assert notices(client, aid) == [(1, "email", "sent")]
    alert = client.get("/v1/social/alerts", headers=ADMIN).json()[0]
    assert alert["status"] == "open" and alert["acknowledged_by"]  # taken, not closed
    assert "channel_alert.acknowledged" in client.get("/v1/audit", headers=ADMIN).text


def test_resolving_stops_the_escalation(app: tuple[TestClient, Orchestrator]) -> None:
    client, orch = app
    contact(client, 1, email="guardia@example.test")
    contact(client, 2, email="respaldo@example.test")
    message(client, CRISIS)
    aid = alert_id(client)
    client.post(f"/v1/social/alerts/{aid}/resolve", headers=ADMIN)
    assert asyncio.run(orch.oncall.escalate_due(utcnow() + timedelta(hours=1))) == 0


def test_a_request_for_a_person_tells_level_one_only(app: tuple[TestClient, Orchestrator]) -> None:
    client, orch = app
    contact(client, 1, email="guardia@example.test")
    contact(client, 2, email="respaldo@example.test")
    message(client, "Quiero hablar con una persona")
    aid = alert_id(client)
    assert asyncio.run(orch.oncall.escalate_due(utcnow() + timedelta(hours=1))) == 0
    assert notices(client, aid) == [(1, "email", "sent")]
    assert FakeSMTP.sent[0]["subject"] == "Alerta: hablar con una persona"


def test_two_replicas_never_notify_twice(app: tuple[TestClient, Orchestrator]) -> None:
    client, orch = app
    contact(client, 1, email="guardia@example.test")
    contact(client, 2, email="respaldo@example.test")
    message(client, CRISIS)
    aid = alert_id(client)
    later = utcnow() + timedelta(minutes=6)

    async def both() -> list[int]:
        return list(
            await asyncio.gather(orch.oncall.escalate_due(later), orch.oncall.escalate_due(later))
        )

    assert sorted(asyncio.run(both())) == [0, 1]
    assert notices(client, aid) == [(1, "email", "sent"), (2, "email", "sent")]


def test_an_empty_list_climbs_to_the_end_and_is_audited(
    app: tuple[TestClient, Orchestrator],
) -> None:
    client, orch = app
    message(client, CRISIS)  # no on-call contacts at all
    aid = alert_id(client)
    for _ in range(oncall_module.MAX_LEVEL + 1):
        asyncio.run(orch.oncall.escalate_due())
    levels = [level for level, channel, outcome in notices(client, aid) if outcome == "none"]
    assert levels == list(range(1, oncall_module.MAX_LEVEL + 1))
    assert "channel_alert.unattended" in client.get("/v1/audit", headers=ADMIN).text


def test_whatsapp_notice_uses_the_approved_template(
    app: tuple[TestClient, Orchestrator], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, orch = app
    seen: list[httpx.Request] = []

    def meta(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"messages": [{"id": "wamid.alert"}]})

    contact(client, 1, whatsapp_number="+1 (555) 000-2222")
    message(client, "Quiero hablar con una persona", "wamid.a")
    aid = alert_id(client)
    assert notices(client, aid) == [(1, "whatsapp", "skipped")]  # no template configured

    orch.settings.whatsapp_alert_template = "alerta_guardia"
    monkeypatch.setenv("SOCIAL_SECRET_WA_DEMO", "EAAG-synthetic")
    orch.oncall.notifier.transport = httpx.MockTransport(meta)
    client.post(f"/v1/social/alerts/{aid}/resolve", headers=ADMIN)
    message(client, CRISIS, "wamid.b")
    open_id = client.get("/v1/social/alerts", headers=ADMIN).json()[0]["alert_id"]
    assert notices(client, open_id) == [(1, "whatsapp", "sent")]
    sent = json.loads(seen[0].content)
    assert sent["to"] == "15550002222" and sent["type"] == "template"
    assert sent["template"]["name"] == "alerta_guardia"
    assert sent["template"]["language"] == {"code": "es"}
    assert sent["template"]["components"][0]["parameters"] == [{"type": "text", "text": open_id}]


def test_a_failing_channel_is_recorded_and_the_others_still_go(
    app: tuple[TestClient, Orchestrator],
) -> None:
    client, _ = app
    FakeSMTP.fail = True
    contact(client, 1, telegram_chat_id="5550001", email="guardia@example.test")
    message(client, CRISIS)
    rows = client.get(f"/v1/social/alerts/{alert_id(client)}/notifications", headers=ADMIN).json()
    by_channel = {r["channel"]: r for r in rows}
    assert by_channel["email"]["outcome"] == "failed" and by_channel["email"]["detail"] == "OSError"
    assert by_channel["telegram"]["outcome"] == "dry_run"


@pytest.mark.parametrize(
    "body",
    [
        {"display_name": "x", "level": 1},  # no channel
        {"display_name": "x", "level": 1, "email": "not-an-email"},
        {"display_name": "x", "level": 1, "whatsapp_number": "123"},
        {"display_name": "x", "level": 0, "email": "a@example.test"},
        {"display_name": "x", "level": 6, "email": "a@example.test"},
    ],
)
def test_bad_contacts_are_refused(
    app: tuple[TestClient, Orchestrator], body: dict[str, Any]
) -> None:
    client, _ = app
    assert client.post("/v1/admin/on-call", json=body, headers=ADMIN).status_code == 422


def test_only_admins_manage_the_list(app: tuple[TestClient, Orchestrator]) -> None:
    client, _ = app
    contact_id = contact(client, 1, email="guardia@example.test")
    staff = client.post(
        "/v1/admin/staff", json={"name": "recepcion", "roles": ["reception"]}, headers=ADMIN
    ).json()
    reception = {"X-API-Key": staff["key"]}
    body = {"display_name": "x", "level": 1, "email": "a@example.test"}
    assert client.post("/v1/admin/on-call", json=body, headers=reception).status_code == 403
    assert len(client.get("/v1/admin/on-call", headers=reception).json()) == 1
    assert client.delete(f"/v1/admin/on-call/{contact_id}", headers=ADMIN).status_code == 204
    assert client.delete(f"/v1/admin/on-call/{contact_id}", headers=ADMIN).status_code == 404


def test_notice_text_carries_no_patient_data() -> None:
    text = notice_text("crisis", "abc123", "https://api.example/")
    assert "CRISIS" in text and "abc123" in text and "https://api.example/" in text
    assert "no hay datos del paciente" in text
