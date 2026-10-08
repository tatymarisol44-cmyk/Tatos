"""Incoming WhatsApp (ADR 0015, M4): Meta's verification and signature, deduplication,
STOP, and alerts to staff for crisis wording, without ever storing message text.
Synthetic numbers only (the 555 range)."""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

import orchestrator.inbound as inbound_module
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


TOKEN = "EAAG-synthetic-wa-token"


def open_alert(client: TestClient) -> str:
    connect(client)
    post(client, payload("Quiero hablar con una persona"))
    return str(client.get("/v1/social/alerts", headers=ADMIN).json()[0]["alert_id"])


def reply(
    client: TestClient, alert_id: str, text: str = "Hola, soy la psicóloga. Te llamo ahora."
) -> Any:
    return client.post(f"/v1/social/alerts/{alert_id}/reply", json={"text": text}, headers=ADMIN)


def test_a_person_replies_inside_the_window(
    app: tuple[TestClient, Orchestrator], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, orch = app
    seen: list[httpx.Request] = []

    def meta(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"messages": [{"id": "wamid.out1"}]})

    monkeypatch.setenv("SOCIAL_SECRET_WA_DEMO", TOKEN)
    orch.inbound.transport = httpx.MockTransport(meta)
    alert_id = open_alert(client)
    done = reply(client, alert_id).json()
    assert done == {"alert_id": alert_id, "outcome": "sent", "message_id": "wamid.out1"}
    sent = json.loads(seen[0].content)
    assert seen[0].url.path == f"/v25.0/{NUMBER_ID}/messages"
    assert sent["to"] == SENDER and sent["type"] == "text"
    assert seen[0].headers["Authorization"] == f"Bearer {TOKEN}"
    audit = client.get("/v1/audit", headers=ADMIN).text
    assert "channel_alert.reply_sent" in audit and "Te llamo" not in audit and TOKEN not in audit


def test_reply_without_a_credential_runs_dry(app: tuple[TestClient, Orchestrator]) -> None:
    client, _ = app
    assert reply(client, open_alert(client)).json()["outcome"] == "dry_run"


def test_reply_is_refused_after_stop(app: tuple[TestClient, Orchestrator]) -> None:
    client, _ = app
    alert_id = open_alert(client)
    post(client, payload("STOP", "wamid.stop"))
    refused = reply(client, alert_id)
    assert refused.status_code == 409 and "WA-OPT-IN" in refused.json()["detail"]
    assert reply(client, "0123456789ab").status_code == 404


def test_reply_outside_the_window_is_refused(
    app: tuple[TestClient, Orchestrator], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = app
    alert_id = open_alert(client)
    later = inbound_module.utcnow() + inbound_module.SERVICE_WINDOW + timedelta(minutes=1)
    monkeypatch.setattr(inbound_module, "utcnow", lambda: later)
    refused = reply(client, alert_id)
    assert refused.status_code == 409 and "WA-WINDOW" in refused.json()["detail"]


@pytest.mark.parametrize(
    ("handler", "outcome"),
    [
        (lambda r: httpx.Response(400, json={"error": {"code": 131047}}), "failed"),
        (lambda r: httpx.Response(200, json={}), "failed"),
    ],
)
def test_reply_failures_are_reported(
    app: tuple[TestClient, Orchestrator],
    monkeypatch: pytest.MonkeyPatch,
    handler: Any,
    outcome: str,
) -> None:
    client, orch = app
    monkeypatch.setenv("SOCIAL_SECRET_WA_DEMO", TOKEN)
    orch.inbound.transport = httpx.MockTransport(handler)
    assert reply(client, open_alert(client)).json()["outcome"] == outcome


def test_reply_transport_error_is_uncertain(
    app: tuple[TestClient, Orchestrator], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, orch = app

    def broken(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    monkeypatch.setenv("SOCIAL_SECRET_WA_DEMO", TOKEN)
    orch.inbound.transport = httpx.MockTransport(broken)
    assert reply(client, open_alert(client)).json()["outcome"] == "uncertain"


def test_reply_needs_an_active_account_and_its_own_tenant(
    app: tuple[TestClient, Orchestrator],
) -> None:
    client, _ = app
    alert_id = open_alert(client)
    assert (
        client.post(
            f"/v1/social/alerts/{alert_id}/reply", json={"text": "x"}, headers=GLOBEX
        ).status_code
        == 404
    )
    account = client.get("/v1/social/accounts", headers=ADMIN).json()[0]["account_id"]
    client.delete(f"/v1/social/accounts/{account}", headers=ADMIN)
    refused = reply(client, alert_id)
    assert refused.status_code == 409 and "disabled" in refused.json()["detail"]


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


# --- warm automatic replies (auto_reply.py, decision P6) ---------------------------------


def sent_texts(orch: Orchestrator, monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Capture what would reach Meta, with a credential configured."""
    seen: list[dict[str, Any]] = []

    def meta(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"messages": [{"id": f"wamid.out.{len(seen)}"}]})

    monkeypatch.setenv("SOCIAL_SECRET_WA_DEMO", "EAAG-synthetic")
    orch.inbound.transport = httpx.MockTransport(meta)
    return seen


def test_a_crisis_gets_warmth_and_the_emergency_lines_at_once(
    app: tuple[TestClient, Orchestrator], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, orch = app
    connect(client)
    seen = sent_texts(orch, monkeypatch)
    assert post(client, payload(CRISIS_TEXT, "wamid.c1")) == 200
    [reply] = seen
    body = reply["text"]["body"]
    assert reply["to"] == SENDER and reply["type"] == "text"
    assert "No estás sola ni solo" in body and "ECU 911" in body and "171, opción 6" in body
    # The alert for a person is opened as before: the reply never replaces it.
    assert len(client.get("/v1/social/alerts", headers=ADMIN).json()) == 1
    # A second crisis message within 10 minutes opens its alert but is not re-answered.
    post(client, payload(CRISIS_TEXT, "wamid.c2"))
    assert len(seen) == 1
    audit = client.get("/v1/audit", headers=ADMIN).text
    assert "channel.auto_reply_sent" in audit and SENDER not in audit and "ECU" not in audit


def test_welcome_menu_once_person_and_stop_confirmations(
    app: tuple[TestClient, Orchestrator], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, orch = app
    orch.settings.whatsapp_ai_replies = False  # the fixed texts; the assistant has its own tests
    connect(client)
    seen = sent_texts(orch, monkeypatch)
    post(client, payload("Hola, ¿atienden los sábados?", "wamid.o1"))
    post(client, payload("¿Y cuánto cuesta?", "wamid.o2"))  # same 12 h: no second menu
    assert len(seen) == 1 and "Gracias por escribir" in seen[0]["text"]["body"]
    post(client, payload("Quiero hablar con una persona", "wamid.h1"))
    assert "una persona del equipo" in seen[-1]["text"]["body"]
    post(client, payload("STOP", "wamid.s1"))
    assert "ya no te enviaremos" in seen[-1]["text"]["body"]
    # After STOP: no more welcome menus, even after the 12 hours.
    later = inbound_module.utcnow() + timedelta(hours=13)
    monkeypatch.setattr(inbound_module, "utcnow", lambda: later)
    before = len(seen)
    post(client, payload("hola de nuevo", "wamid.o3"))
    assert len(seen) == before
    # But a crisis is always answered, opted out or not.
    post(client, payload(CRISIS_TEXT, "wamid.c9"))
    assert "ECU 911" in seen[-1]["text"]["body"]


def test_without_a_credential_or_when_disabled(
    app: tuple[TestClient, Orchestrator], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, orch = app
    connect(client)
    post(client, payload(CRISIS_TEXT, "wamid.d1"))  # no SOCIAL_SECRET: recorded, not sent
    assert "channel.auto_reply_dry_run" in client.get("/v1/audit", headers=ADMIN).text
    orch.settings.whatsapp_auto_reply = False
    seen = sent_texts(orch, monkeypatch)
    post(client, payload("hola", "wamid.d2"))
    assert seen == []
