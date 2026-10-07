"""Incoming WhatsApp (ADR 0015, M4): Meta's verification and signature, deduplication,
STOP, and alerts to staff for crisis wording, without ever storing message text.
Synthetic numbers only (the 555 range)."""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from orchestrator.api.app import create_app
from orchestrator.catalog import Catalog
from orchestrator.config import Settings
from orchestrator.crisis import classify
from orchestrator.llm import FakeLLM
from orchestrator.service import Orchestrator

ADMIN = {"X-API-Key": "test-key"}
GLOBEX = {"X-API-Key": "other-key"}
VERIFY = "verify-synthetic"
SECRET = "app-secret-synthetic"
NUMBER_ID = "106540352242922"
SENDER = "15550001111"
CRISIS_TEXT = "Ya no quiero vivir, pienso en quitarme la vida"


# --- classification ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "intent"),
    [
        (CRISIS_TEXT, "crisis"),
        ("A veces pienso en el SUICIDIO", "crisis"),
        ("quiero hacerme daño", "crisis"),
        ("I want to die", "crisis"),
        ("STOP", "stop"),
        ("baja", "stop"),
        ("Quiero hablar con una persona", "human"),
        ("¿Puedo hablar con la psicóloga?", "human"),
        ("Hola, ¿a qué hora atienden el sábado?", "other"),
        ("Me muero de ganas de empezar la terapia", "other"),
    ],
)
def test_classify(text: str, intent: str) -> None:
    assert classify(text) == intent


# --- webhook ---------------------------------------------------------------------------


@pytest.fixture
def app(settings: Settings, catalog: Catalog) -> Iterator[tuple[TestClient, Orchestrator]]:
    settings.whatsapp_verify_token = SecretStr(VERIFY)
    settings.meta_app_secret = SecretStr(SECRET)
    orch = Orchestrator(settings, catalog=catalog, llm=FakeLLM())
    with TestClient(create_app(settings, orch)) as c:
        yield c, orch


def connect(client: TestClient, headers: dict[str, str] = ADMIN) -> None:
    body = {
        "network": "whatsapp",
        "external_id": NUMBER_ID,
        "handle": "+1 555 000 0000",
        "secret_ref": "WA_DEMO",
    }
    assert client.post("/v1/social/accounts", json=body, headers=headers).status_code == 201


def payload(
    text: str, message_id: str = "wamid.1", number_id: str = NUMBER_ID, kind: str = "text"
) -> bytes:
    message: dict[str, Any] = {
        "from": SENDER,
        "id": message_id,
        "timestamp": "1790000000",
        "type": kind,
    }
    if kind == "text":
        message["text"] = {"body": text}
    doc = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "WABA_ID",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {
                                "display_phone_number": "15550000000",
                                "phone_number_id": number_id,
                            },
                            "contacts": [{"profile": {"name": "Synthetic"}, "wa_id": SENDER}],
                            "messages": [message],
                        },
                    }
                ],
            }
        ],
    }
    return json.dumps(doc).encode()


def post(client: TestClient, body: bytes, secret: str = SECRET) -> int:
    signature = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    headers = {"X-Hub-Signature-256": signature, "Content-Type": "application/json"}
    return client.post("/v1/channels/whatsapp", content=body, headers=headers).status_code


def test_meta_verification_handshake(app: tuple[TestClient, Orchestrator]) -> None:
    client, _ = app
    ok = client.get(
        "/v1/channels/whatsapp",
        params={"hub.mode": "subscribe", "hub.verify_token": VERIFY, "hub.challenge": "1158201444"},
    )
    assert ok.status_code == 200 and ok.text == "1158201444"
    bad = client.get(
        "/v1/channels/whatsapp",
        params={"hub.mode": "subscribe", "hub.verify_token": "wrong", "hub.challenge": "1"},
    )
    assert bad.status_code == 403


def test_unconfigured_endpoint_does_not_exist(settings: Settings, catalog: Catalog) -> None:
    orch = Orchestrator(settings, catalog=catalog, llm=FakeLLM())
    with TestClient(create_app(settings, orch)) as client:
        assert client.get("/v1/channels/whatsapp").status_code == 404
        assert client.post("/v1/channels/whatsapp", content=b"{}").status_code == 404


def test_unsigned_or_wrongly_signed_notifications_are_refused(
    app: tuple[TestClient, Orchestrator],
) -> None:
    client, _ = app
    connect(client)
    body = payload(CRISIS_TEXT)
    assert client.post("/v1/channels/whatsapp", content=body).status_code == 401
    assert post(client, body, secret="someone-else") == 401
    assert client.get("/v1/social/alerts", headers=ADMIN).json() == []


def test_crisis_opens_one_alert_and_the_text_is_never_stored(
    app: tuple[TestClient, Orchestrator],
) -> None:
    client, _ = app
    connect(client)
    body = payload(CRISIS_TEXT)
    assert post(client, body) == 200
    assert post(client, body) == 200  # Meta retries: the same message id is handled once
    alerts = client.get("/v1/social/alerts", headers=ADMIN)
    assert [(a["kind"], a["address"], a["status"]) for a in alerts.json()] == [
        ("crisis", SENDER, "open")
    ]
    audit = client.get("/v1/audit", headers=ADMIN).text
    assert "channel_alert.crisis" in audit and "channel_alert.read" in audit
    for haystack in (alerts.text, audit):
        assert "quitarme la vida" not in haystack and "vivir" not in haystack


def test_requests_for_a_person_open_an_alert_and_can_be_resolved(
    app: tuple[TestClient, Orchestrator],
) -> None:
    client, _ = app
    connect(client)
    post(client, payload("Quiero hablar con una persona"))
    alert = client.get("/v1/social/alerts", headers=ADMIN).json()[0]
    assert alert["kind"] == "human"
    resolve = f"/v1/social/alerts/{alert['alert_id']}/resolve"
    assert client.post(resolve, headers=ADMIN).status_code == 204
    assert client.post(resolve, headers=ADMIN).status_code == 404
    assert client.get("/v1/social/alerts", headers=ADMIN).json() == []
    resolved = client.get("/v1/social/alerts?state=resolved", headers=ADMIN).json()
    assert resolved[0]["resolved_by"]


async def test_stop_records_a_pseudonymous_opt_out(app: tuple[TestClient, Orchestrator]) -> None:
    client, orch = app
    connect(client)
    assert post(client, payload("STOP")) == 200
    assert await orch.inbound.is_opted_out("acme", "whatsapp", "+1 (555) 000-1111")
    assert not await orch.inbound.is_opted_out("acme", "whatsapp", "15550002222")
    assert not await orch.inbound.is_opted_out("globex", "whatsapp", SENDER)
    assert client.get("/v1/social/alerts", headers=ADMIN).json() == []
    assert SENDER not in client.get("/v1/audit", headers=ADMIN).text  # only a key prefix


def test_other_messages_and_unknown_accounts_open_nothing(
    app: tuple[TestClient, Orchestrator],
) -> None:
    client, _ = app
    connect(client)
    assert post(client, payload("¿A qué hora atienden?", "wamid.2")) == 200
    assert post(client, payload("", "wamid.3", kind="image")) == 200
    assert post(client, payload(CRISIS_TEXT, "wamid.4", number_id="999")) == 200  # not ours
    assert post(client, b"not json") == 200
    assert client.get("/v1/social/alerts", headers=ADMIN).json() == []


def test_an_account_claimed_by_two_tenants_routes_nowhere(
    app: tuple[TestClient, Orchestrator],
) -> None:
    client, _ = app
    connect(client)
    connect(client, GLOBEX)
    assert post(client, payload(CRISIS_TEXT)) == 200
    assert client.get("/v1/social/alerts", headers=ADMIN).json() == []
    assert client.get("/v1/social/alerts", headers=GLOBEX).json() == []


def test_alerts_are_for_care_staff_and_their_own_tenant(
    app: tuple[TestClient, Orchestrator],
) -> None:
    client, _ = app
    connect(client)
    post(client, payload(CRISIS_TEXT))
    marketing = client.post(
        "/v1/admin/staff", json={"name": "mkt", "roles": ["marketing"]}, headers=ADMIN
    ).json()
    assert (
        client.get("/v1/social/alerts", headers={"X-API-Key": marketing["key"]}).status_code == 403
    )
    assert client.get("/v1/social/alerts", headers=GLOBEX).json() == []
