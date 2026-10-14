"""The inbox (ADR 0023): Instagram Direct and Facebook Messenger arrive through the same
signed Meta webhook as WhatsApp; every message is sealed at rest; only the clinician
reads; the AI only drafts, and a person sends. Synthetic ids and numbers only."""

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
from sqlalchemy import select

import orchestrator.inbox as inbox_module
from orchestrator.api.app import create_app
from orchestrator.api.channels import _social_messages
from orchestrator.catalog import Catalog
from orchestrator.config import Settings
from orchestrator.inbox import inbox_conversations, inbox_messages
from orchestrator.llm import FakeLLM
from orchestrator.sealing import Sealer, SealError
from orchestrator.service import Orchestrator

ADMIN = {"X-API-Key": "test-key"}
GLOBEX = {"X-API-Key": "other-key"}
VERIFY = "verify-synthetic"
SECRET = "app-secret-synthetic"
TOKEN = "IGAA-synthetic-token"
IG_ID = "17841400000000001"  # our professional Instagram account
PAGE_ID = "100000000000002"  # our Facebook Page
NUMBER_ID = "106540352242922"  # our WhatsApp number id
IG_SENDER = "990000000000011"  # Instagram-scoped id of who writes
FB_SENDER = "990000000000022"  # Page-scoped id of who writes
WA_SENDER = "15550001111"
CRISIS_TEXT = "Ya no quiero vivir, pienso en quitarme la vida"
QUESTION = "Hola, ¿atienden los sábados por la mañana?"


@pytest.fixture
def app(settings: Settings, catalog: Catalog) -> Iterator[tuple[TestClient, Orchestrator]]:
    settings.whatsapp_verify_token = SecretStr(VERIFY)
    settings.meta_app_secret = SecretStr(SECRET)
    orch = Orchestrator(settings, catalog=catalog, llm=FakeLLM())
    with TestClient(create_app(settings, orch)) as c:
        for network, external_id in (
            ("instagram", IG_ID),
            ("facebook", PAGE_ID),
            ("whatsapp", NUMBER_ID),
        ):
            body = {
                "network": network,
                "external_id": external_id,
                "handle": f"consultorio-{network}",
                "secret_ref": "META_DEMO",
            }
            assert c.post("/v1/social/accounts", json=body, headers=ADMIN).status_code == 201
        yield c, orch


def clinician(client: TestClient, name: str = "dra.demo") -> dict[str, str]:
    staff = client.post(
        "/v1/admin/staff", json={"name": name, "roles": ["reviewer"]}, headers=ADMIN
    ).json()
    return {"X-API-Key": staff["key"]}


def social(network: str, text: str | None, mid: str, sender: str, echo: bool = False) -> bytes:
    """A Messenger ("page") or Instagram ("instagram") notification, as Meta sends it."""
    ours = IG_ID if network == "instagram" else PAGE_ID
    message: dict[str, Any] = {"mid": mid}
    if text is not None:
        message["text"] = text
    else:
        message["attachments"] = [{"type": "image", "payload": {"url": "https://x.invalid/a"}}]
    if echo:
        message["is_echo"] = True
    event = {
        "sender": {"id": ours if echo else sender},
        "recipient": {"id": sender if echo else ours},
        "timestamp": 1790000000000,
        "message": message,
    }
    obj = "instagram" if network == "instagram" else "page"
    doc = {"object": obj, "entry": [{"id": ours, "time": 1790000000000, "messaging": [event]}]}
    return json.dumps(doc).encode()


def whatsapp(text: str, mid: str) -> bytes:
    message = {"from": WA_SENDER, "id": mid, "type": "text", "text": {"body": text}}
    value = {"metadata": {"phone_number_id": NUMBER_ID}, "messages": [message]}
    doc = {
        "object": "whatsapp_business_account",
        "entry": [{"id": "WABA", "changes": [{"field": "messages", "value": value}]}],
    }
    return json.dumps(doc).encode()


def post(client: TestClient, body: bytes, secret: str = SECRET) -> int:
    signature = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    headers = {"X-Hub-Signature-256": signature, "Content-Type": "application/json"}
    return client.post("/v1/channels/meta", content=body, headers=headers).status_code


def meta(orch: Orchestrator, monkeypatch: pytest.MonkeyPatch, status: int = 200) -> list[Any]:
    """Capture what would reach Meta (channel auto-replies and inbox replies alike)."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if status != 200:
            return httpx.Response(status, json={"error": {"message": "synthetic"}})
        if "whatsapp" in json.loads(request.content).get("messaging_product", ""):
            return httpx.Response(200, json={"messages": [{"id": f"wamid.out.{len(seen)}"}]})
        return httpx.Response(200, json={"recipient_id": "x", "message_id": f"m.out.{len(seen)}"})

    monkeypatch.setenv("SOCIAL_SECRET_META_DEMO", TOKEN)
    orch.inbound.transport = orch.inbox.transport = httpx.MockTransport(handler)
    return seen


def only(client: TestClient, headers: dict[str, str], network: str) -> dict[str, Any]:
    [found] = client.get(f"/v1/inbox?network={network}", headers=headers).json()
    return found  # type: ignore[no-any-return]


# --- the webhook ------------------------------------------------------------------------


def test_instagram_and_messenger_events_are_read_from_the_meta_payload() -> None:
    assert _social_messages(json.loads(social("instagram", "hola", "m1", IG_SENDER))) == [
        ("instagram", IG_ID, "m1", IG_SENDER, "hola")
    ]
    assert _social_messages(json.loads(social("facebook", "hola", "m2", FB_SENDER))) == [
        ("facebook", PAGE_ID, "m2", FB_SENDER, "hola")
    ]
    # A photo is recorded without text; our own sends (echoes) and other objects are not.
    assert _social_messages(json.loads(social("facebook", None, "m3", FB_SENDER)))[0][4] == ""
    assert _social_messages(json.loads(social("instagram", "x", "m4", IG_SENDER, True))) == []
    assert _social_messages({"object": "user", "entry": []}) == []
    assert _social_messages(["not", "a", "dict"]) == []


def test_messages_from_the_three_networks_land_in_one_inbox(
    app: tuple[TestClient, Orchestrator],
) -> None:
    client, _ = app
    dra = clinician(client)
    assert post(client, social("instagram", QUESTION, "ig.1", IG_SENDER)) == 200
    assert post(client, social("instagram", QUESTION, "ig.1", IG_SENDER)) == 200  # Meta retry
    assert post(client, social("facebook", None, "fb.1", FB_SENDER)) == 200
    assert post(client, whatsapp("Buenas tardes", "wamid.1")) == 200
    listed = client.get("/v1/inbox", headers=dra).json()
    assert sorted(c["network"] for c in listed) == ["facebook", "instagram", "whatsapp"]
    instagram = only(client, dra, "instagram")
    assert instagram["unread"] == 1 and instagram["preview"] == QUESTION
    assert only(client, dra, "facebook")["preview"] == "[adjunto]"
    opened = client.get(f"/v1/inbox/{instagram['conversation_id']}", headers=dra).json()
    assert [m["text"] for m in opened["messages"]] == [QUESTION]
    assert opened["window_open"] is True and opened["unread"] == 0
    assert only(client, dra, "instagram")["unread"] == 0


def test_unsigned_notifications_reach_nothing(app: tuple[TestClient, Orchestrator]) -> None:
    client, _ = app
    body = social("instagram", QUESTION, "ig.x", IG_SENDER)
    assert client.post("/v1/channels/meta", content=body).status_code == 401
    assert post(client, body, secret="someone-else") == 401
    assert client.get("/v1/inbox", headers=clinician(client)).json() == []


async def test_the_database_never_holds_a_readable_text_or_sender(
    app: tuple[TestClient, Orchestrator],
) -> None:
    client, orch = app
    post(client, social("instagram", CRISIS_TEXT, "ig.2", IG_SENDER))
    post(client, whatsapp(QUESTION, "wamid.2"))
    async with orch.db.engine.connect() as conn:
        rows = [
            *(await conn.execute(select(inbox_messages))).all(),
            *(await conn.execute(select(inbox_conversations))).all(),
        ]
    stored = repr(rows)
    assert len(rows) >= 4
    for secret in ("vivir", "sábados", IG_SENDER, WA_SENDER):
        assert secret not in stored


def test_only_the_clinician_reads_and_every_read_is_audited(
    app: tuple[TestClient, Orchestrator],
) -> None:
    client, _ = app
    post(client, social("facebook", QUESTION, "fb.2", FB_SENDER))
    reception = client.post(
        "/v1/admin/staff", json={"name": "recepcion", "roles": ["reception"]}, headers=ADMIN
    ).json()
    for headers in (ADMIN, {"X-API-Key": reception["key"]}):
        assert client.get("/v1/inbox", headers=headers).status_code == 403
    dra = clinician(client)
    conversation_id = only(client, dra, "facebook")["conversation_id"]
    for headers in (ADMIN, {"X-API-Key": reception["key"]}):
        assert client.get(f"/v1/inbox/{conversation_id}", headers=headers).status_code == 403
    client.get(f"/v1/inbox/{conversation_id}", headers=dra)
    audit = client.get("/v1/audit", headers=ADMIN).text
    assert "inbox.list" in audit and "inbox.read" in audit
    assert "sábados" not in audit and FB_SENDER not in audit


def test_another_practice_sees_nothing(app: tuple[TestClient, Orchestrator]) -> None:
    client, _ = app
    post(client, social("instagram", QUESTION, "ig.3", IG_SENDER))
    conversation_id = only(client, clinician(client), "instagram")["conversation_id"]
    other = client.post(
        "/v1/admin/staff", json={"name": "dr.otro", "roles": ["reviewer"]}, headers=GLOBEX
    ).json()
    theirs = {"X-API-Key": other["key"]}
    assert client.get("/v1/inbox", headers=theirs).json() == []
    assert client.get(f"/v1/inbox/{conversation_id}", headers=theirs).status_code == 404
    reply = {"body": "hola"}
    assert (
        client.post(f"/v1/inbox/{conversation_id}/reply", json=reply, headers=theirs).status_code
        == 404
    )


# --- automatic replies on Instagram and Facebook: fixed texts only ----------------------


def test_a_crisis_on_instagram_gets_the_emergency_lines_and_a_person(
    app: tuple[TestClient, Orchestrator], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, orch = app
    seen = meta(orch, monkeypatch)
    assert post(client, social("instagram", CRISIS_TEXT, "ig.c1", IG_SENDER)) == 200
    [sent] = seen
    assert sent.url.host == "graph.instagram.com" and sent.url.path == f"/{IG_ID}/messages"
    assert sent.headers["Authorization"] == f"Bearer {TOKEN}"
    body = json.loads(sent.content)
    assert body["recipient"] == {"id": IG_SENDER} and body["messaging_type"] == "RESPONSE"
    assert "ECU 911" in body["message"]["text"]
    alerts = client.get("/v1/social/alerts", headers=ADMIN).json()
    assert [(a["kind"], a["network"]) for a in alerts] == [("crisis", "instagram")]
    dra = clinician(client)
    conversation = only(client, dra, "instagram")
    assert conversation["attention"] == "crisis"
    opened = client.get(f"/v1/inbox/{conversation['conversation_id']}", headers=dra).json()
    assert [(m["direction"], m["status"], m["author"]) for m in opened["messages"]] == [
        ("in", "received", None),
        ("out", "sent", "auto:crisis"),
    ]
    # The AI never drafts where a person must answer.
    draft = client.post(f"/v1/inbox/{conversation['conversation_id']}/draft", headers=dra)
    assert draft.status_code == 409


def test_everyday_questions_on_social_get_no_automatic_reply(
    app: tuple[TestClient, Orchestrator], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, orch = app
    seen = meta(orch, monkeypatch)
    post(client, social("facebook", QUESTION, "fb.3", FB_SENDER))
    post(client, social("instagram", QUESTION, "ig.4", IG_SENDER))
    assert seen == []
    orch.settings.social_auto_reply = False
    post(client, social("instagram", CRISIS_TEXT, "ig.c2", IG_SENDER))
    assert seen == []  # switched off: the alert still opens for a person
    assert client.get("/v1/social/alerts", headers=ADMIN).json()[0]["kind"] == "crisis"


# --- drafts and replies -----------------------------------------------------------------


def test_the_ai_drafts_and_the_professional_sends(
    app: tuple[TestClient, Orchestrator], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, orch = app
    seen = meta(orch, monkeypatch)
    post(client, social("facebook", QUESTION, "fb.4", FB_SENDER))
    dra = clinician(client)
    conversation_id = only(client, dra, "facebook")["conversation_id"]
    draft = client.post(f"/v1/inbox/{conversation_id}/draft", headers=dra)
    assert draft.status_code == 201
    suggestion = draft.json()
    assert suggestion["status"] == "draft" and "Gracias por escribirnos" in suggestion["text"]
    assert "Responde 1, 2 o 3" not in suggestion["text"]  # only WhatsApp books by number
    assert seen == []  # a draft is never sent by itself
    edited = suggestion["text"] + " Sí, atendemos los sábados de 9 a 13."
    sent = client.post(
        f"/v1/inbox/{conversation_id}/reply",
        json={"body": edited, "draft_id": suggestion["message_id"]},
        headers=dra,
    )
    assert sent.status_code == 201 and sent.json()["status"] == "sent"
    [request] = seen
    assert request.url.host == "graph.facebook.com"
    assert request.url.path == f"/v25.0/{PAGE_ID}/messages"
    assert json.loads(request.content)["message"]["text"] == edited
    messages = client.get(f"/v1/inbox/{conversation_id}", headers=dra).json()["messages"]
    assert [(m["direction"], m["status"]) for m in messages] == [
        ("in", "received"),
        ("out", "used"),
        ("out", "sent"),
    ]
    audit = client.get("/v1/audit", headers=ADMIN).text
    assert "inbox.draft" in audit and "inbox.reply_sent" in audit
    assert "sábados" not in audit and TOKEN not in audit


def test_an_unsafe_suggestion_is_withheld(app: tuple[TestClient, Orchestrator]) -> None:
    client, orch = app
    assert isinstance(orch.llm, FakeLLM)
    orch.llm.whatsapp_reply = "Te recomiendo tomar sertralina 50 mg cada noche."
    post(client, social("instagram", QUESTION, "ig.5", IG_SENDER))
    dra = clinician(client)
    conversation_id = only(client, dra, "instagram")["conversation_id"]
    response = client.post(f"/v1/inbox/{conversation_id}/draft", headers=dra)
    assert response.status_code == 409 and "write the reply yourself" in response.text


def test_a_social_id_is_never_taken_for_a_phone_number(
    app: tuple[TestClient, Orchestrator], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, orch = app
    assert orch.inbox.assistant is not None
    looked_up: list[str] = []

    async def spy(tenant: str, phone: str) -> None:
        looked_up.append(phone)

    monkeypatch.setattr(orch.inbox.assistant, "patient_for", spy)
    orch.settings.whatsapp_auto_reply = False  # only the drafts look the sender up here
    post(client, social("instagram", QUESTION, "ig.6", IG_SENDER))
    post(client, whatsapp(QUESTION, "wamid.6"))
    dra = clinician(client)
    for network in ("instagram", "whatsapp"):
        conversation_id = only(client, dra, network)["conversation_id"]
        assert client.post(f"/v1/inbox/{conversation_id}/draft", headers=dra).status_code == 201
    assert looked_up == [WA_SENDER]


def test_drafts_can_be_discarded(app: tuple[TestClient, Orchestrator]) -> None:
    client, _ = app
    post(client, social("instagram", QUESTION, "ig.7", IG_SENDER))
    dra = clinician(client)
    conversation_id = only(client, dra, "instagram")["conversation_id"]
    draft_id = client.post(f"/v1/inbox/{conversation_id}/draft", headers=dra).json()["message_id"]
    path = f"/v1/inbox/{conversation_id}/drafts/{draft_id}"
    assert client.delete(path, headers=dra).status_code == 204
    assert client.delete(path, headers=dra).status_code == 404
    messages = client.get(f"/v1/inbox/{conversation_id}", headers=dra).json()["messages"]
    assert [m["direction"] for m in messages] == ["in"]


def test_a_whatsapp_reply_from_the_inbox(
    app: tuple[TestClient, Orchestrator], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, orch = app
    orch.settings.whatsapp_auto_reply = False
    seen = meta(orch, monkeypatch)
    post(client, whatsapp(QUESTION, "wamid.7"))
    dra = clinician(client)
    conversation_id = only(client, dra, "whatsapp")["conversation_id"]
    reply = {"body": "Sí, de 9 a 13."}
    done = client.post(f"/v1/inbox/{conversation_id}/reply", json=reply, headers=dra).json()
    assert done["status"] == "sent" and done["external_id"] == "wamid.out.1"
    [request] = seen
    assert request.url.path == f"/v25.0/{NUMBER_ID}/messages"
    assert json.loads(request.content)["to"] == WA_SENDER


def test_replies_without_a_credential_run_dry_and_failures_are_reported(
    app: tuple[TestClient, Orchestrator], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, orch = app
    post(client, social("instagram", QUESTION, "ig.8", IG_SENDER))
    dra = clinician(client)
    path = f"/v1/inbox/{only(client, dra, 'instagram')['conversation_id']}/reply"
    reply = {"body": "Hola, te escribo enseguida."}
    assert client.post(path, json=reply, headers=dra).json()["status"] == "dry_run"
    meta(orch, monkeypatch, status=400)
    assert client.post(path, json=reply, headers=dra).json()["status"] == "failed"

    def broken(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("synthetic", request=request)

    orch.inbox.transport = httpx.MockTransport(broken)
    assert client.post(path, json=reply, headers=dra).json()["status"] == "uncertain"
    assert client.post(path, json={"body": "   "}, headers=dra).status_code == 409


def test_replies_are_refused_after_24_hours_or_after_stop(
    app: tuple[TestClient, Orchestrator], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = app
    post(client, social("facebook", QUESTION, "fb.5", FB_SENDER))
    dra = clinician(client)
    path = f"/v1/inbox/{only(client, dra, 'facebook')['conversation_id']}/reply"
    reply = {"body": "Hola"}
    later = inbox_module.utcnow() + timedelta(hours=25)
    with monkeypatch.context() as m:
        m.setattr(inbox_module, "utcnow", lambda: later)
        closed = client.post(path, json=reply, headers=dra)
        assert closed.status_code == 409 and "24-hour" in closed.text
    post(client, social("facebook", "STOP", "fb.6", FB_SENDER))
    stopped = client.post(path, json=reply, headers=dra)
    assert stopped.status_code == 409 and "not to be contacted" in stopped.text


def test_link_to_a_patient_close_and_the_patient_rights(
    app: tuple[TestClient, Orchestrator],
) -> None:
    client, _ = app
    patient = {"id": "p-inbox", "display_name": "Paciente Demo"}
    assert client.post("/v1/crm/patients", json=patient, headers=ADMIN).status_code == 201
    post(client, social("instagram", "Quiero hablar con una persona", "ig.9", IG_SENDER))
    dra = clinician(client)
    conversation = only(client, dra, "instagram")
    assert conversation["attention"] == "human"
    base = f"/v1/inbox/{conversation['conversation_id']}"
    unknown = client.put(f"{base}/patient", json={"patient_id": "nadie"}, headers=dra)
    assert unknown.status_code == 404
    linked = client.put(f"{base}/patient", json={"patient_id": "p-inbox"}, headers=dra)
    assert linked.status_code == 200
    assert client.post(f"{base}/close", headers=dra).status_code == 204
    closed = only(client, dra, "instagram")
    assert (closed["status"], closed["attention"], closed["patient_id"]) == (
        "closed",
        None,
        "p-inbox",
    )
    # Access and erasure requests include the linked conversations.
    exported = client.get("/v1/subjects/p-inbox/export", headers=ADMIN).json()
    [held] = exported["inbox_conversations"]
    # What she wrote, and the fixed "a person will write to you" (dry run: no credential).
    assert [m["direction"] for m in held["messages"]] == ["in", "out"]
    assert held["messages"][0]["text"] == "Quiero hablar con una persona"
    erased = client.delete("/v1/subjects/p-inbox", headers=ADMIN).json()
    assert erased["inbox_conversations"] == 1
    assert client.get("/v1/inbox", headers=dra).json() == []


async def test_retention_removes_old_messages(app: tuple[TestClient, Orchestrator]) -> None:
    client, orch = app
    post(client, social("facebook", QUESTION, "fb.10", FB_SENDER))
    dra = clinician(client)
    # Nothing is 90 days old yet; everything is older than "now".
    assert await orch.inbox.purge(timedelta(days=90)) == 0
    assert await orch.inbox.purge(timedelta(0), dry_run=True) == 1
    assert await orch.inbox.purge(timedelta(0)) == 1
    assert client.get("/v1/inbox", headers=dra).json() == []
    assert "inbox_messages" in await orch.retention(timedelta(days=30), dry_run=True)


# --- sealing ----------------------------------------------------------------------------


def test_sealed_text_opens_only_with_its_key_and_tenant() -> None:
    sealer = Sealer(b"k" * 32)
    sealed = sealer.seal("acme", "hola, ¿qué tal?")
    assert sealed.startswith("v1:") and "hola" not in sealed
    assert sealer.open("acme", sealed) == "hola, ¿qué tal?"
    assert sealer.seal("acme", "hola") != sealer.seal("acme", "hola")  # fresh nonce each time
    with pytest.raises(SealError):
        sealer.open("globex", sealed)  # moved to another practice's row
    with pytest.raises(SealError):
        Sealer(b"x" * 32).open("acme", sealed)  # another key
    tampered = sealed[:-4] + ("AAAA" if not sealed.endswith("AAAA") else "BBBB")
    with pytest.raises(SealError):
        sealer.open("acme", tampered)
    with pytest.raises(SealError):
        sealer.open("acme", "texto plano")
