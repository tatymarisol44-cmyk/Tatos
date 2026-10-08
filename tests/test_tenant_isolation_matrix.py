"""Adversarial tenant-isolation matrix (audit 2026-10-08, item 1).

Tenant `acme` is seeded with one of every resource the API manages, each carrying a
marker string. Then the strongest attacker of another tenant, `globex`'s admin service
key (plus a globex patient key for the `/v1/me` routes), calls EVERY operation in the
OpenAPI schema with acme's ids in the path and in the body. Three properties must hold:

1. No answer contains an acme marker or an acme id (nothing leaks, not even in errors).
2. An operation addressed to an acme resource is refused (403/404), never served.
3. Afterwards acme's data is exactly as it was (no cross-tenant write landed).

`test_every_operation_is_in_the_matrix` makes a new endpoint fail CI until it is added
here, so the matrix cannot silently fall behind the API. Synthetic data only."""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from orchestrator.api.app import create_app
from orchestrator.catalog import Catalog
from orchestrator.config import Settings
from orchestrator.db import utcnow
from orchestrator.llm import FakeLLM
from orchestrator.service import Orchestrator

ACME = {"X-API-Key": "test-key"}
GLOBEX = {"X-API-Key": "other-key"}
MARKER = "Zacarias-Acme-Secreto"
DOC_MARKER = "Protocolo-Acme-Confidencial"
NOTE_MARKER = "Nota-Acme-Clinica"
APP_SECRET = "app-secret-synthetic"
NUMBER_ID = "106540352242999"
GOOD = "Hola {first_name}, te esperamos para tu control. Responde STOP para salir."
BRIEF = {
    "title": "Cuidar tu sonrisa es salud",
    "points": ["Una revision a tiempo ayuda."],
    "cta": "Agenda tu cita en linea",
    "practice_name": "Consultorio Demo",
}

# Operations that address no tenant data; they still go through the leak scan.
UNSCOPED = {
    ("GET", "/healthz"),
    ("GET", "/readyz"),
    ("GET", "/.well-known/agent-card.json"),
    ("GET", "/v1/agents"),
    ("GET", "/v1/social/rules"),
    ("POST", "/v1/route"),
    ("GET", "/v1/clinical/instrument-templates"),
}


@dataclass
class Acme:
    ids: dict[str, str] = field(default_factory=dict)

    def secrets(self) -> list[str]:
        """Strings that must never reach a globex caller."""
        return [MARKER, DOC_MARKER, NOTE_MARKER, *self.ids.values()]


def _signed(client: TestClient, body: bytes) -> int:
    signature = "sha256=" + hmac.new(APP_SECRET.encode(), body, hashlib.sha256).hexdigest()
    headers = {"X-Hub-Signature-256": signature, "Content-Type": "application/json"}
    return client.post("/v1/channels/whatsapp", content=body, headers=headers).status_code


def _crisis_message() -> bytes:
    message = {
        "from": "15550002222",
        "id": "wamid.acme-1",
        "timestamp": "1790000000",
        "type": "text",
        "text": {"body": "Ya no quiero vivir"},
    }
    value = {
        "messaging_product": "whatsapp",
        "metadata": {"display_phone_number": "15550000000", "phone_number_id": NUMBER_ID},
        "contacts": [{"profile": {"name": "Synthetic"}, "wa_id": "15550002222"}],
        "messages": [message],
    }
    doc = {
        "object": "whatsapp_business_account",
        "entry": [{"id": "WABA", "changes": [{"field": "messages", "value": value}]}],
    }
    return json.dumps(doc).encode()


def _ok(response: Any) -> Any:
    assert response.status_code < 300, f"{response.request.url}: {response.text}"
    return response.json() if response.content else None


def seed(c: TestClient) -> Acme:
    acme = Acme()
    ids = acme.ids
    ids["patient"] = "p-acme-77"
    _ok(
        c.post(
            "/v1/crm/patients",
            json={"id": ids["patient"], "display_name": MARKER, "telegram_chat_id": "7"},
            headers=ACME,
        )
    )
    for purpose in ("marketing", "analytics"):
        _ok(
            c.put(
                f"/v1/subjects/{ids['patient']}/consents/{purpose}",
                json={"granted": True, "source": "form"},
                headers=ACME,
            )
        )
    past = (utcnow() - timedelta(days=400)).isoformat()
    appt = _ok(
        c.post(
            "/v1/crm/appointments",
            json={"patient_id": ids["patient"], "starts_at": past},
            headers=ACME,
        )
    )
    ids["appointment"] = appt["id"]
    _ok(
        c.post(
            f"/v1/crm/appointments/{appt['id']}/status",
            json={"status": "completed"},
            headers=ACME,
        )
    )
    treatment = _ok(
        c.post(
            "/v1/crm/treatments",
            json={"patient_id": ids["patient"], "title": "Ortodoncia", "amount": 900},
            headers=ACME,
        )
    )
    ids["treatment"] = treatment["id"]
    campaign = _ok(
        c.post(
            "/v1/campaigns",
            json={
                "name": "Vuelve",
                "kind": "reactivation",
                "segment": "dormant",
                "template": GOOD,
                "mode": "simulation",
            },
            headers=ACME,
        )
    )
    ids["campaign"] = campaign["id"]
    doc = _ok(
        c.post(
            "/v1/knowledge/documents",
            json={"title": "Protocolo", "text": f"{DOC_MARKER}: esterilizar a 134 grados."},
            headers=ACME,
        )
    )
    ids["doc"] = doc["doc_id"]
    staff = _ok(
        c.post("/v1/admin/staff", json={"name": "dra.acme", "roles": ["reviewer"]}, headers=ACME)
    )
    ids["principal"] = staff["id"]
    clinician = {"X-API-Key": staff["key"]}
    _ok(
        c.post(
            "/v1/admin/professionals",
            json={
                "professional_id": "dra-acme",
                "display_name": "Dra Acme",
                "pack_id": "ec-psychologist",
                "staff_id": "dra.acme",
            },
            headers=ACME,
        )
    )
    ids["professional"] = "dra-acme"
    every_day = [
        {"day": d, "hours": "08:00-20:00", "slot_minutes": 60}
        for d in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
    ]
    _ok(c.put("/v1/agenda/professionals/dra-acme/hours", json={"hours": every_day}, headers=ACME))
    slot = _ok(c.get("/v1/agenda/slots", params={"professional_id": "dra-acme"}, headers=ACME))[0]
    _ok(
        c.post(
            "/v1/crm/appointments",
            json={
                "patient_id": ids["patient"],
                "starts_at": slot["starts_at"],
                "duration_min": 60,
                "professional_id": "dra-acme",
            },
            headers=ACME,
        )
    )
    note = _ok(
        c.post(
            f"/v1/clinical/patients/{ids['patient']}/documents",
            json={"doc_type": "session_note", "body": NOTE_MARKER},
            headers=clinician,
        )
    )
    ids["clinical_doc"] = note["document_id"]
    test = _ok(
        c.post(
            "/v1/clinical/instruments/from-template",
            json={"template": "gad7"},
            headers=clinician,
        )
    )
    ids["instrument"] = test["instrument_id"]
    result = _ok(
        c.post(
            f"/v1/clinical/patients/{ids['patient']}/instrument-results",
            json={
                "instrument_id": test["instrument_id"],
                "answers": {f"i{n}": 1 for n in range(1, 8)},
            },
            headers=clinician,
        )
    )
    ids["instrument_result"] = result["result_id"]
    uploaded = c.post(
        f"/v1/clinical/patients/{ids['patient']}/files",
        files={"file": ("consent.pdf", b"%PDF-1.7 " + MARKER.encode(), "application/pdf")},
        data={"label": "Consentimiento"},
        headers=clinician,
    )
    ids["file"] = _ok(uploaded)["file_id"]
    # A held answer creates a review on an acme thread.
    held = _ok(
        c.post(
            "/v1/chat",
            json={
                "question": "¿Qué dosis de ibuprofeno tomo?",
                "thread_id": "acme-thread-1",
                "subject_id": ids["patient"],
            },
            headers=ACME,
        )
    )
    assert held["status"] == "pending_review"
    ids["thread"] = "acme-thread-1"
    wa = _ok(
        c.post(
            "/v1/social/accounts",
            json={
                "network": "whatsapp",
                "external_id": NUMBER_ID,
                "handle": "+1 555 000 0000",
                "secret_ref": "WA_ACME",
            },
            headers=ACME,
        )
    )
    ids["wa_account"] = wa["account_id"]
    ig = _ok(
        c.post(
            "/v1/social/accounts",
            json={
                "network": "instagram",
                "external_id": "ig-acme-1",
                "handle": "@acme",
                "secret_ref": "IG_ACME",
            },
            headers=ACME,
        )
    )
    ids["ig_account"] = ig["account_id"]
    pub = _ok(
        c.post(
            "/v1/social/publications",
            json={
                "account_id": ig["account_id"],
                "kind": "infographic",
                "caption": "Tu sonrisa importa.",
                "brief": BRIEF,
            },
            headers=ACME,
        )
    )
    ids["publication"] = pub["publication_id"]
    contact = _ok(
        c.post(
            "/v1/admin/on-call",
            json={"display_name": "Guardia Acme", "level": 1, "email": "guardia@acme.test"},
            headers=ACME,
        )
    )
    ids["contact"] = contact["contact_id"]
    assert _signed(c, _crisis_message()) == 200
    [alert] = _ok(c.get("/v1/social/alerts", headers=ACME))
    ids["alert"] = alert["alert_id"]
    return acme


def snapshot(c: TestClient, acme: Acme) -> dict[str, Any]:
    """Everything acme can see, read with acme's key."""
    ids = acme.ids

    def get(path: str) -> Any:
        return _ok(c.get(path, headers=ACME))

    return {
        "patient": get(f"/v1/crm/patients/{ids['patient']}"),
        "consents": get(f"/v1/subjects/{ids['patient']}/consents"),
        "appointments": get("/v1/crm/appointments"),
        "treatments": get("/v1/crm/treatments"),
        "campaign": get(f"/v1/campaigns/{ids['campaign']}"),
        "docs": get("/v1/knowledge/documents"),
        "principals": get("/v1/admin/principals"),
        "professionals": get("/v1/admin/professionals"),
        "clinical": get(f"/v1/clinical/patients/{ids['patient']}/documents"),
        "instruments": get("/v1/clinical/instruments"),
        "instrument_results": get(f"/v1/clinical/patients/{ids['patient']}/instrument-results"),
        "files": get(f"/v1/clinical/patients/{ids['patient']}/files"),
        "agenda": get("/v1/agenda?days=14"),
        "hours": get(f"/v1/agenda/professionals/{ids['professional']}/hours"),
        "reviews": get("/v1/reviews"),
        "thread": get(f"/v1/threads/{ids['thread']}"),
        "accounts": get("/v1/social/accounts"),
        "publication": get(f"/v1/social/publications/{ids['publication']}"),
        "on_call": get("/v1/admin/on-call"),
        "alerts": get("/v1/social/alerts"),
    }


def attacks(acme: Acme) -> dict[tuple[str, str], tuple[str, dict[str, Any] | None]]:
    """(method, template) -> (concrete path with acme ids, body). Every body is valid, so a
    refusal comes from tenant scoping, not from request validation."""
    i = acme.ids
    p, t = i["patient"], i["thread"]
    return {
        ("GET", "/healthz"): ("/healthz", None),
        ("GET", "/readyz"): ("/readyz", None),
        ("GET", "/.well-known/agent-card.json"): ("/.well-known/agent-card.json", None),
        ("GET", "/v1/agents"): ("/v1/agents", None),
        ("POST", "/v1/route"): ("/v1/route", {"question": MARKER}),
        ("POST", "/a2a"): (
            "/a2a",
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "message/send",
                "params": {
                    "message": {
                        "role": "user",
                        "messageId": "m1",
                        "contextId": t,
                        "parts": [{"kind": "text", "text": "resume la conversacion"}],
                    }
                },
            },
        ),
        ("POST", "/v1/chat"): ("/v1/chat", {"question": "¿Qué dije antes?", "thread_id": t}),
        ("POST", "/v1/chat/stream"): (
            "/v1/chat/stream",
            {"question": "¿Qué dije antes?", "thread_id": t},
        ),
        ("POST", "/v1/knowledge/search"): ("/v1/knowledge/search", {"query": DOC_MARKER}),
        ("GET", "/v1/knowledge/documents"): ("/v1/knowledge/documents", None),
        ("POST", "/v1/knowledge/documents"): (
            "/v1/knowledge/documents",
            {"title": "x", "text": "texto de globex"},
        ),
        ("DELETE", "/v1/knowledge/documents/{doc_id}"): (
            f"/v1/knowledge/documents/{i['doc']}",
            None,
        ),
        ("GET", "/v1/threads/{thread_id}"): (f"/v1/threads/{t}", None),
        ("DELETE", "/v1/threads/{thread_id}"): (f"/v1/threads/{t}", None),
        ("DELETE", "/v1/threads"): ("/v1/threads", None),
        ("GET", "/v1/reviews"): ("/v1/reviews", None),
        ("GET", "/v1/reviews/{thread_id}"): (f"/v1/reviews/{t}", None),
        ("POST", "/v1/reviews/{thread_id}"): (f"/v1/reviews/{t}", {"approved": True}),
        ("PUT", "/v1/subjects/{subject_id}/consents/{purpose}"): (
            f"/v1/subjects/{p}/consents/marketing",
            {"granted": False, "source": "form"},
        ),
        ("GET", "/v1/subjects/{subject_id}/consents"): (f"/v1/subjects/{p}/consents", None),
        ("GET", "/v1/subjects/{subject_id}/export"): (f"/v1/subjects/{p}/export", None),
        ("DELETE", "/v1/subjects/{subject_id}"): (f"/v1/subjects/{p}", None),
        ("GET", "/v1/audit"): ("/v1/audit", None),
        ("GET", "/v1/audit/verify"): ("/v1/audit/verify", None),
        ("POST", "/v1/admin/staff"): ("/v1/admin/staff", {"name": "intruso", "roles": ["admin"]}),
        ("GET", "/v1/admin/principals"): ("/v1/admin/principals", None),
        ("DELETE", "/v1/admin/principals/{principal_id}"): (
            f"/v1/admin/principals/{i['principal']}",
            None,
        ),
        ("POST", "/v1/admin/principals/{principal_id}/sign-out"): (
            f"/v1/admin/principals/{i['principal']}/sign-out",
            None,
        ),
        ("POST", "/v1/crm/patients/{patient_id}/access"): (
            f"/v1/crm/patients/{p}/access",
            None,
        ),
        ("POST", "/v1/crm/patients"): (
            "/v1/crm/patients",
            {"id": "p-globex-1", "display_name": "Paciente Globex"},
        ),
        ("GET", "/v1/crm/patients"): ("/v1/crm/patients", None),
        ("GET", "/v1/crm/patients/{patient_id}"): (f"/v1/crm/patients/{p}", None),
        ("PATCH", "/v1/crm/patients/{patient_id}"): (
            f"/v1/crm/patients/{p}",
            {"display_name": "pwned"},
        ),
        ("POST", "/v1/crm/appointments"): (
            "/v1/crm/appointments",
            {"patient_id": p, "starts_at": (utcnow() + timedelta(days=3)).isoformat()},
        ),
        ("GET", "/v1/crm/appointments"): ("/v1/crm/appointments", None),
        ("POST", "/v1/crm/appointments/{appointment_id}/status"): (
            f"/v1/crm/appointments/{i['appointment']}/status",
            {"status": "cancelled"},
        ),
        ("POST", "/v1/crm/treatments"): (
            "/v1/crm/treatments",
            {"patient_id": p, "title": "x", "amount": 1},
        ),
        ("GET", "/v1/crm/treatments"): ("/v1/crm/treatments", None),
        ("POST", "/v1/crm/treatments/{treatment_id}/stage"): (
            f"/v1/crm/treatments/{i['treatment']}/stage",
            {"stage": "accepted"},
        ),
        ("GET", "/v1/crm/alerts"): ("/v1/crm/alerts", None),
        ("GET", "/v1/insights/summary"): ("/v1/insights/summary", None),
        ("GET", "/v1/insights/segments"): ("/v1/insights/segments", None),
        ("POST", "/v1/insights/ask"): ("/v1/insights/ask", {"question": "¿Cuántos pacientes?"}),
        ("POST", "/v1/campaigns"): (
            "/v1/campaigns",
            {"name": "g", "kind": "education", "segment": "dormant", "template": GOOD},
        ),
        ("GET", "/v1/campaigns"): ("/v1/campaigns", None),
        ("GET", "/v1/campaigns/{campaign_id}"): (f"/v1/campaigns/{i['campaign']}", None),
        ("PUT", "/v1/campaigns/{campaign_id}/template"): (
            f"/v1/campaigns/{i['campaign']}/template",
            {"template": GOOD + " "},
        ),
        ("POST", "/v1/campaigns/{campaign_id}/approve"): (
            f"/v1/campaigns/{i['campaign']}/approve",
            {},
        ),
        ("POST", "/v1/campaigns/{campaign_id}/send"): (
            f"/v1/campaigns/{i['campaign']}/send",
            None,
        ),
        ("POST", "/v1/campaigns/{campaign_id}/retry"): (
            f"/v1/campaigns/{i['campaign']}/retry",
            None,
        ),
        ("POST", "/v1/campaigns/{campaign_id}/recipients/{patient_id}/delivery"): (
            f"/v1/campaigns/{i['campaign']}/recipients/{p}/delivery",
            {"delivered": True},
        ),
        ("POST", "/v1/campaigns/{campaign_id}/cancel"): (
            f"/v1/campaigns/{i['campaign']}/cancel",
            None,
        ),
        ("GET", "/v1/campaigns/{campaign_id}/results"): (
            f"/v1/campaigns/{i['campaign']}/results",
            None,
        ),
        ("GET", "/v1/social/rules"): ("/v1/social/rules", None),
        ("GET", "/v1/social/accounts"): ("/v1/social/accounts", None),
        ("POST", "/v1/social/accounts"): (
            "/v1/social/accounts",
            {
                # acme's WhatsApp number: claiming it must not hijack acme's inbound.
                "network": "whatsapp",
                "external_id": NUMBER_ID,
                "handle": "+1 555 000 0000",
                "secret_ref": "WA_GLOBEX",
            },
        ),
        ("DELETE", "/v1/social/accounts/{account_id}"): (
            f"/v1/social/accounts/{i['ig_account']}",
            None,
        ),
        ("GET", "/v1/social/alerts"): ("/v1/social/alerts", None),
        ("POST", "/v1/social/alerts/{alert_id}/reply"): (
            f"/v1/social/alerts/{i['alert']}/reply",
            {"text": "hola"},
        ),
        ("POST", "/v1/social/alerts/{alert_id}/ack"): (f"/v1/social/alerts/{i['alert']}/ack", None),
        ("GET", "/v1/social/alerts/{alert_id}/notifications"): (
            f"/v1/social/alerts/{i['alert']}/notifications",
            None,
        ),
        ("POST", "/v1/social/alerts/{alert_id}/resolve"): (
            f"/v1/social/alerts/{i['alert']}/resolve",
            None,
        ),
        ("GET", "/v1/social/publications"): ("/v1/social/publications", None),
        ("POST", "/v1/social/publications"): (
            "/v1/social/publications",
            {
                "account_id": i["ig_account"],
                "kind": "infographic",
                "caption": "Globex.",
                "brief": BRIEF,
            },
        ),
        ("GET", "/v1/social/publications/{publication_id}"): (
            f"/v1/social/publications/{i['publication']}",
            None,
        ),
        ("POST", "/v1/social/publications/{publication_id}/approve"): (
            f"/v1/social/publications/{i['publication']}/approve",
            None,
        ),
        ("POST", "/v1/social/publications/{publication_id}/publish"): (
            f"/v1/social/publications/{i['publication']}/publish",
            None,
        ),
        ("POST", "/v1/social/publications/{publication_id}/cancel"): (
            f"/v1/social/publications/{i['publication']}/cancel",
            None,
        ),
        ("GET", "/v1/admin/professionals"): ("/v1/admin/professionals", None),
        ("POST", "/v1/admin/professionals"): (
            "/v1/admin/professionals",
            # acme's professional id and acme's staff id: must stay in globex.
            {
                "professional_id": i["professional"],
                "display_name": "x",
                "pack_id": "ec-psychologist",
                "staff_id": "dra.acme",
            },
        ),
        ("DELETE", "/v1/admin/professionals/{professional_id}"): (
            f"/v1/admin/professionals/{i['professional']}",
            None,
        ),
        ("GET", "/v1/clinical/patients/{patient_id}/documents"): (
            f"/v1/clinical/patients/{p}/documents",
            None,
        ),
        ("POST", "/v1/clinical/patients/{patient_id}/documents"): (
            f"/v1/clinical/patients/{p}/documents",
            {"doc_type": "session_note", "body": "intruso"},
        ),
        ("GET", "/v1/clinical/documents/{document_id}"): (
            f"/v1/clinical/documents/{i['clinical_doc']}",
            None,
        ),
        ("GET", "/v1/clinical/instrument-templates"): ("/v1/clinical/instrument-templates", None),
        ("POST", "/v1/clinical/instruments"): (
            "/v1/clinical/instruments",
            {
                "spec": {
                    "name": "Globex",
                    "items": [
                        {
                            "id": "q",
                            "text": "x",
                            "options": [{"label": "no", "value": 0}, {"label": "si", "value": 1}],
                        }
                    ],
                    "licence": {"source": "own", "attestation": True},
                }
            },
        ),
        ("POST", "/v1/clinical/instruments/from-template"): (
            "/v1/clinical/instruments/from-template",
            {"template": "phq9"},
        ),
        ("GET", "/v1/clinical/instruments"): ("/v1/clinical/instruments", None),
        ("GET", "/v1/clinical/instruments/{instrument_id}"): (
            f"/v1/clinical/instruments/{i['instrument']}",
            None,
        ),
        ("PUT", "/v1/clinical/instruments/{instrument_id}"): (
            f"/v1/clinical/instruments/{i['instrument']}",
            {
                "spec": {
                    "name": "pwned",
                    "items": [
                        {
                            "id": "q",
                            "text": "x",
                            "options": [{"label": "no", "value": 0}, {"label": "si", "value": 1}],
                        }
                    ],
                    "licence": {"source": "own", "attestation": True},
                }
            },
        ),
        ("DELETE", "/v1/clinical/instruments/{instrument_id}"): (
            f"/v1/clinical/instruments/{i['instrument']}",
            None,
        ),
        ("POST", "/v1/clinical/patients/{patient_id}/instrument-results"): (
            f"/v1/clinical/patients/{p}/instrument-results",
            {"instrument_id": i["instrument"], "answers": {f"i{n}": 0 for n in range(1, 8)}},
        ),
        ("GET", "/v1/clinical/patients/{patient_id}/instrument-results"): (
            f"/v1/clinical/patients/{p}/instrument-results",
            None,
        ),
        ("GET", "/v1/clinical/instrument-results/{result_id}"): (
            f"/v1/clinical/instrument-results/{i['instrument_result']}",
            None,
        ),
        ("POST", "/v1/clinical/patients/{patient_id}/files"): (
            f"/v1/clinical/patients/{p}/files",
            {"__multipart__": True, "label": "intruso"},
        ),
        ("GET", "/v1/clinical/patients/{patient_id}/files"): (
            f"/v1/clinical/patients/{p}/files",
            None,
        ),
        ("GET", "/v1/clinical/files/{file_id}"): (f"/v1/clinical/files/{i['file']}", None),
        ("GET", "/v1/usage"): ("/v1/usage", None),
        ("GET", "/v1/clinical/follow-up"): ("/v1/clinical/follow-up", None),
        ("GET", "/v1/clinical/patients/{patient_id}/follow-up"): (
            f"/v1/clinical/patients/{p}/follow-up",
            None,
        ),
        ("GET", "/v1/agenda"): ("/v1/agenda?days=14", None),
        ("GET", "/v1/agenda/professionals/{professional_id}/hours"): (
            f"/v1/agenda/professionals/{i['professional']}/hours",
            None,
        ),
        ("PUT", "/v1/agenda/professionals/{professional_id}/hours"): (
            f"/v1/agenda/professionals/{i['professional']}/hours",
            {"hours": [{"day": "mon", "hours": "01:00-02:00", "slot_minutes": 60}]},
        ),
        ("GET", "/v1/agenda/slots"): (
            f"/v1/agenda/slots?professional_id={i['professional']}",
            None,
        ),
        ("POST", "/v1/agenda/professionals/{professional_id}/calendar-link"): (
            f"/v1/agenda/professionals/{i['professional']}/calendar-link",
            None,
        ),
        ("GET", "/v1/admin/on-call"): ("/v1/admin/on-call", None),
        ("POST", "/v1/admin/on-call"): (
            "/v1/admin/on-call",
            {"display_name": "Guardia Globex", "level": 1},
        ),
        ("DELETE", "/v1/admin/on-call/{contact_id}"): (
            f"/v1/admin/on-call/{i['contact']}",
            None,
        ),
    }


def patient_attacks(acme: Acme) -> dict[tuple[str, str], tuple[str, dict[str, Any] | None]]:
    """The `/v1/me` routes, called with a globex PATIENT key."""
    t = acme.ids["thread"]
    return {
        ("GET", "/v1/me"): ("/v1/me", None),
        ("GET", "/v1/me/appointments"): ("/v1/me/appointments", None),
        ("GET", "/v1/me/treatments"): ("/v1/me/treatments", None),
        ("GET", "/v1/me/loyalty"): ("/v1/me/loyalty", None),
        ("POST", "/v1/me/offers/seen"): ("/v1/me/offers/seen", None),
        ("GET", "/v1/me/consents"): ("/v1/me/consents", None),
        ("PUT", "/v1/me/consents/{purpose}"): ("/v1/me/consents/marketing", {"granted": False}),
        ("POST", "/v1/me/chat"): ("/v1/me/chat", {"question": "¿Qué dije?", "thread_id": t}),
        ("GET", "/v1/me/chat/{thread_id}"): (f"/v1/me/chat/{t}", None),
        ("GET", "/v1/me/slots"): ("/v1/me/slots?professional_id=dra-acme", None),
        ("POST", "/v1/me/appointments"): (
            "/v1/me/appointments",
            {"professional_id": "dra-acme", "starts_at": "2030-01-07T14:00:00+00:00"},
        ),
        ("GET", "/v1/me/calendar-link"): ("/v1/me/calendar-link", None),
    }


# Operations addressed to an acme resource: these must be refused outright. The rest are
# collection or "own namespace" operations: they may succeed, but only on globex's data.
ADDRESSED = {
    key
    for key in (
        ("DELETE", "/v1/knowledge/documents/{doc_id}"),
        ("GET", "/v1/reviews/{thread_id}"),
        ("POST", "/v1/reviews/{thread_id}"),
        ("DELETE", "/v1/admin/principals/{principal_id}"),
        ("POST", "/v1/admin/principals/{principal_id}/sign-out"),
        ("POST", "/v1/crm/patients/{patient_id}/access"),
        ("GET", "/v1/crm/patients/{patient_id}"),
        ("PATCH", "/v1/crm/patients/{patient_id}"),
        ("POST", "/v1/crm/appointments"),
        ("POST", "/v1/crm/appointments/{appointment_id}/status"),
        ("POST", "/v1/crm/treatments"),
        ("POST", "/v1/crm/treatments/{treatment_id}/stage"),
        ("GET", "/v1/campaigns/{campaign_id}"),
        ("PUT", "/v1/campaigns/{campaign_id}/template"),
        ("POST", "/v1/campaigns/{campaign_id}/approve"),
        ("POST", "/v1/campaigns/{campaign_id}/send"),
        ("POST", "/v1/campaigns/{campaign_id}/retry"),
        ("POST", "/v1/campaigns/{campaign_id}/recipients/{patient_id}/delivery"),
        ("POST", "/v1/campaigns/{campaign_id}/cancel"),
        ("GET", "/v1/campaigns/{campaign_id}/results"),
        ("DELETE", "/v1/social/accounts/{account_id}"),
        ("POST", "/v1/social/alerts/{alert_id}/reply"),
        ("POST", "/v1/social/alerts/{alert_id}/ack"),
        ("GET", "/v1/social/alerts/{alert_id}/notifications"),
        ("POST", "/v1/social/alerts/{alert_id}/resolve"),
        ("POST", "/v1/social/publications"),
        ("GET", "/v1/social/publications/{publication_id}"),
        ("POST", "/v1/social/publications/{publication_id}/approve"),
        ("POST", "/v1/social/publications/{publication_id}/publish"),
        ("POST", "/v1/social/publications/{publication_id}/cancel"),
        ("DELETE", "/v1/admin/professionals/{professional_id}"),
        ("GET", "/v1/clinical/patients/{patient_id}/documents"),
        ("POST", "/v1/clinical/patients/{patient_id}/documents"),
        ("GET", "/v1/clinical/documents/{document_id}"),
        ("DELETE", "/v1/admin/on-call/{contact_id}"),
        ("GET", "/v1/clinical/instruments/{instrument_id}"),
        ("PUT", "/v1/clinical/instruments/{instrument_id}"),
        ("DELETE", "/v1/clinical/instruments/{instrument_id}"),
        ("POST", "/v1/clinical/patients/{patient_id}/instrument-results"),
        ("GET", "/v1/clinical/patients/{patient_id}/instrument-results"),
        ("GET", "/v1/clinical/instrument-results/{result_id}"),
        ("POST", "/v1/clinical/patients/{patient_id}/files"),
        ("GET", "/v1/clinical/patients/{patient_id}/follow-up"),
        ("GET", "/v1/agenda/professionals/{professional_id}/hours"),
        ("PUT", "/v1/agenda/professionals/{professional_id}/hours"),
        ("GET", "/v1/agenda/slots"),
        ("POST", "/v1/agenda/professionals/{professional_id}/calendar-link"),
        ("GET", "/v1/me/slots"),
        ("POST", "/v1/me/appointments"),
        ("GET", "/v1/clinical/patients/{patient_id}/files"),
        ("GET", "/v1/clinical/files/{file_id}"),
    )
}
# Ids that live in each tenant's own namespace, so globex may use the same string for its
# own data: a thread id (keyed `tenant:thread`; globex chats on "acme-thread-1" earlier in
# the sweep, creating its OWN thread) and a subject id (consents and the access export
# exist for subjects without a CRM row, e.g. a lead). They may answer 2xx, but only with
# globex's data: the leak scan and the before/after snapshot of acme are what prove it.
NAMESPACED = {
    ("GET", "/v1/threads/{thread_id}"),
    ("PUT", "/v1/subjects/{subject_id}/consents/{purpose}"),
    ("GET", "/v1/subjects/{subject_id}/export"),
}
REFUSED = {403, 404, 409, 422}


@pytest.fixture
def world(
    settings: Settings, catalog: Catalog, tmp_path: Any
) -> Iterator[tuple[TestClient, Acme, dict[str, str]]]:
    settings.tenant_packs = {"acme": "dental", "globex": "dental"}
    settings.campaign_default_holdout_pct = 0
    settings.media_dir = tmp_path / "media"
    settings.meta_app_secret = SecretStr(APP_SECRET)
    settings.whatsapp_verify_token = SecretStr("verify-synthetic")
    orch = Orchestrator(settings, catalog=catalog, llm=FakeLLM())
    with TestClient(create_app(settings, orch)) as c:
        acme = seed(c)
        _ok(
            c.post(
                "/v1/crm/patients",
                json={"id": "p-globex-0", "display_name": "Globex Paciente"},
                headers=GLOBEX,
            )
        )
        access = _ok(c.post("/v1/crm/patients/p-globex-0/access", headers=GLOBEX))
        yield c, acme, {"X-API-Key": access["key"]}


def schema_operations(client: TestClient) -> set[tuple[str, str]]:
    paths = client.app.openapi()["paths"]  # type: ignore[attr-defined]
    return {(m.upper(), p) for p, ops in paths.items() for m in ops}


def test_every_operation_is_in_the_matrix(
    world: tuple[TestClient, Acme, dict[str, str]],
) -> None:
    client, acme, _ = world
    covered = set(attacks(acme)) | set(patient_attacks(acme))
    missing = schema_operations(client) - covered
    assert not missing, f"add these operations to the isolation matrix: {sorted(missing)}"
    assert covered >= ADDRESSED | UNSCOPED | NAMESPACED
    assert not ADDRESSED & NAMESPACED


def _call(client: TestClient, method: str, path: str, body: Any, headers: dict[str, str]) -> Any:
    if method == "GET":
        return client.get(path, headers=headers)
    if method == "DELETE":
        return client.delete(path, headers=headers)
    if isinstance(body, dict) and body.get("__multipart__"):
        form = {k: v for k, v in body.items() if k != "__multipart__"}
        upload = {"file": ("x.pdf", b"%PDF-1.7 intruso", "application/pdf")}
        return client.post(path, files=upload, data=form, headers=headers)
    return client.request(method, path, json=body, headers=headers)


def test_another_tenant_can_neither_read_nor_change_acme(
    world: tuple[TestClient, Acme, dict[str, str]],
) -> None:
    client, acme, globex_patient = world
    before = snapshot(client, acme)
    secrets = acme.secrets()
    served: list[str] = []
    leaks: list[str] = []
    calls = [
        (headers, key, path, body)
        for headers, table in ((GLOBEX, attacks(acme)), (globex_patient, patient_attacks(acme)))
        for key, (path, body) in table.items()
    ]

    def sent(path: str, body: Any) -> str:
        return path + json.dumps(body, ensure_ascii=False)

    # Calls that carry no acme id run first, so their answers (the audit log, lists)
    # cannot hold an id merely because globex itself sent it in an earlier call.
    calls.sort(key=lambda c: any(i in sent(c[2], c[3]) for i in acme.ids.values()))
    for headers, (method, template), path, body in calls:
        response = _call(client, method, path, body, headers)
        text = response.text
        assert response.status_code < 500, f"{method} {path}: {response.status_code} {text}"
        # Echoing what the caller sent ("campaign X not found") reveals nothing new.
        leaked = [s for s in secrets if s in text and s not in sent(path, body)]
        if leaked:
            leaks.append(f"{method} {path} -> {leaked}")
        if (method, template) in ADDRESSED and response.status_code not in REFUSED:
            served.append(f"{method} {path} -> {response.status_code}")
    assert not leaks, "acme data reached globex:\n" + "\n".join(leaks)
    assert not served, "globex was served an acme resource:\n" + "\n".join(served)
    assert snapshot(client, acme) == before, "a globex call changed acme's data"
    # acme's WhatsApp number still routes to acme only.
    assert [a["alert_id"] for a in _ok(client.get("/v1/social/alerts", headers=ACME))] == [
        acme.ids["alert"]
    ]


def test_patient_keys_never_cross_patients(
    world: tuple[TestClient, Acme, dict[str, str]],
) -> None:
    """A patient key of acme sees its own record only, not another acme patient's."""
    client, acme, _ = world
    _ok(
        client.post(
            "/v1/crm/patients",
            json={"id": "p-acme-other", "display_name": "Otro Paciente"},
            headers=ACME,
        )
    )
    key = _ok(client.post("/v1/crm/patients/p-acme-other/access", headers=ACME))["key"]
    other = {"X-API-Key": key}
    me = _ok(client.get("/v1/me", headers=other))
    assert MARKER not in json.dumps(me)
    for path in ("/v1/me/appointments", "/v1/me/treatments", "/v1/me/consents"):
        assert acme.ids["patient"] not in client.get(path, headers=other).text
    assert client.get(f"/v1/me/chat/{acme.ids['thread']}", headers=other).status_code == 404
    # Staff routes are closed to patient keys altogether.
    for path in (f"/v1/crm/patients/{acme.ids['patient']}", "/v1/crm/patients", "/v1/audit"):
        assert client.get(path, headers=other).status_code in {401, 403}


# Tables whose primary key does not start with `tenant`, and why that is safe.
GLOBAL_KEYS = {
    # A global surrogate id; the chain order is UNIQUE (tenant, seq) and every read
    # filters on tenant.
    "audit_events": ("tenant", "seq"),
    # Meta message ids are globally unique: deduplication must be global, or the same
    # delivery could be counted once per tenant that claims the number.
    "inbound_events": None,
}


def test_every_table_is_keyed_by_tenant() -> None:
    """Defence in depth under the matrix: a new table cannot forget its tenant column,
    and its key cannot let two tenants' rows collide."""
    import orchestrator.service  # noqa: F401  (registers every table on the metadata)
    from orchestrator.db import metadata

    for table in metadata.tables.values():
        assert "tenant" in table.c, f"{table.name} has no tenant column"
        assert not table.c.tenant.nullable, f"{table.name}.tenant is nullable"
        pk = [c.name for c in table.primary_key.columns]
        if table.name not in GLOBAL_KEYS:
            assert pk[0] == "tenant", f"{table.name}: primary key {pk} must start with tenant"
            continue
        unique = GLOBAL_KEYS[table.name]
        if unique is not None:
            uniques = {
                tuple(c.name for c in con.columns)
                for con in table.constraints
                if con.__class__.__name__ == "UniqueConstraint"
            }
            assert unique in uniques, f"{table.name} lost UNIQUE {unique}"
