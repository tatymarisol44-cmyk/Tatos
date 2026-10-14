"""Tests answered by the patient in their app (RF-TST-03): the clinician sends a
self-report instrument, the patient answers it and never sees the score, an alert answer
opens a crisis alert and gives the emergency lines at once. Synthetic data only."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from orchestrator.api.app import create_app
from orchestrator.catalog import Catalog
from orchestrator.config import Settings
from orchestrator.llm import FakeLLM
from orchestrator.service import Orchestrator

ADMIN = {"X-API-Key": "test-key"}
GLOBEX = {"X-API-Key": "other-key"}


@pytest.fixture
def client(settings: Settings, catalog: Catalog) -> Iterator[TestClient]:
    orch = Orchestrator(settings, catalog=catalog, llm=FakeLLM())
    with TestClient(create_app(settings, orch)) as c:
        for pid, phone in (("p-1", "0991234567"), ("p-2", None)):
            body = {"id": pid, "display_name": "Paciente Demo", "phone": phone}
            assert c.post("/v1/crm/patients", json=body, headers=ADMIN).status_code == 201
        yield c


def key(client: TestClient, headers: dict[str, str], body: dict[str, Any]) -> dict[str, str]:
    return {"X-API-Key": client.post("/v1/admin/staff", json=body, headers=headers).json()["key"]}


def patient(client: TestClient, pid: str = "p-1") -> dict[str, str]:
    return {"X-API-Key": client.post(f"/v1/crm/patients/{pid}/access", headers=ADMIN).json()["key"]}


def phq9(client: TestClient, dra: dict[str, str], official: bool = True) -> str:
    """The PHQ-9 template, with the items' text copied in (synthetic here) when `official`."""
    made = client.post(
        "/v1/clinical/instruments/from-template", json={"template": "phq9"}, headers=dra
    ).json()
    if official:
        spec = made["spec"]
        for n, item in enumerate(spec["items"], 1):
            item["text"] = f"Pregunta sintética {n}"
        revised = client.put(
            f"/v1/clinical/instruments/{made['instrument_id']}", json={"spec": spec}, headers=dra
        )
        assert revised.status_code == 200, revised.text
    return str(made["instrument_id"])


def send(client: TestClient, dra: dict[str, str], instrument_id: str, pid: str = "p-1") -> Any:
    return client.post(
        f"/v1/clinical/patients/{pid}/assignments",
        json={"instrument_id": instrument_id, "due_days": 7},
        headers=dra,
    )


def answers(item9: int = 0) -> dict[str, Any]:
    return {"answers": {**{f"i{n}": 1 for n in range(1, 9)}, "i9": item9}}


@pytest.fixture
def dra(client: TestClient) -> dict[str, str]:
    return key(client, ADMIN, {"name": "dra.vera", "roles": ["reviewer"]})


def test_the_patient_answers_and_never_sees_the_score(
    client: TestClient, dra: dict[str, str]
) -> None:
    sent = send(client, dra, phq9(client, dra))
    assert sent.status_code == 201 and sent.json()["status"] == "pending"
    assert send(client, dra, sent.json()["instrument_id"]).status_code == 422  # already waiting
    me = patient(client)
    [test] = client.get("/v1/me/tests", headers=me).json()
    assert test["name"].startswith("PHQ-9") and len(test["items"]) == 9
    assert test["items"][0]["text"] == "Pregunta sintética 1"
    assert test["items"][0]["options"][0]["label"] == "Nunca"
    assert "scoring" not in test and "band" not in str(test).lower()

    incomplete = client.post(
        f"/v1/me/tests/{test['assignment_id']}", json={"answers": {"i1": 1}}, headers=me
    )
    assert incomplete.status_code == 422
    done = client.post(f"/v1/me/tests/{test['assignment_id']}", json=answers(), headers=me)
    assert done.status_code == 200
    reply = done.json()
    assert reply["status"] == "received" and "help" not in reply
    for hidden in ("total", "band", "severity", "mild"):
        assert hidden not in done.text
    # Answered once: gone from the list, and a second answer is refused.
    assert client.get("/v1/me/tests", headers=me).json() == []
    again = client.post(f"/v1/me/tests/{test['assignment_id']}", json=answers(), headers=me)
    assert again.status_code == 404

    # The professional sees the result, scored with the version sent, by the patient.
    [result] = client.get("/v1/clinical/patients/p-1/instrument-results", headers=dra).json()
    assert result["total"] == 8 and result["band"] == "mild" and result["note"] == "self-report"
    [assignment] = client.get("/v1/clinical/patients/p-1/assignments", headers=dra).json()
    assert assignment["status"] == "completed" and assignment["result_id"] == result["result_id"]
    audit = client.get("/v1/audit", headers=ADMIN).text
    assert "instrument.assigned" in audit and "instrument.self_reported" in audit
    assert "Pregunta sintética" not in audit


def test_an_alert_answer_opens_a_crisis_alert_and_gives_help(
    client: TestClient, dra: dict[str, str]
) -> None:
    send(client, dra, phq9(client, dra))
    me = patient(client)
    [test] = client.get("/v1/me/tests", headers=me).json()
    reply = client.post(f"/v1/me/tests/{test['assignment_id']}", json=answers(2), headers=me)
    body = reply.json()
    assert "ECU 911" in body["help"] and "171, opción 6" in body["help"]
    assert "Ítem" not in body["help"] and "autolesión" not in reply.text  # no clinical wording
    [alert] = client.get("/v1/social/alerts", headers=ADMIN).json()
    assert (alert["kind"], alert["network"], alert["subject_id"]) == ("crisis", "app", "p-1")
    assert alert["address"] == "0991234567"  # to call back
    # An alert from the app cannot be answered as a message: call the patient.
    answer = client.post(
        f"/v1/social/alerts/{alert['alert_id']}/reply", json={"text": "hola"}, headers=ADMIN
    )
    assert answer.status_code == 409


def test_only_ready_self_report_instruments_are_sent(
    client: TestClient, dra: dict[str, str]
) -> None:
    placeholder = send(client, dra, phq9(client, dra, official=False))
    assert placeholder.status_code == 422 and "official text" in placeholder.text
    own = client.post(
        "/v1/clinical/instruments",
        json={
            "spec": {
                "name": "Entrevista breve",
                "administration": "professional",
                "items": [
                    {
                        "id": "a",
                        "text": "¿Cómo dormiste?",
                        "options": [{"label": "Bien", "value": 0}, {"label": "Mal", "value": 1}],
                    }
                ],
                "licence": {"source": "own", "attestation": True},
            }
        },
        headers=dra,
    ).json()
    refused = send(client, dra, own["instrument_id"])
    assert refused.status_code == 422 and "applied by the professional" in refused.text
    assert send(client, dra, "no-such-test").status_code == 404
    assert send(client, dra, phq9(client, dra), pid="p-404").status_code == 404


def test_cancel_and_who_may_do_what(client: TestClient, dra: dict[str, str]) -> None:
    instrument_id = phq9(client, dra)
    assignment_id = send(client, dra, instrument_id).json()["assignment_id"]
    reception = key(client, ADMIN, {"name": "recepcion", "roles": ["reception"]})
    for headers in (ADMIN, reception, patient(client)):
        assert send(client, headers, instrument_id).status_code == 403
        listed = client.get("/v1/clinical/patients/p-1/assignments", headers=headers)
        assert listed.status_code == 403
    # Another patient sees nothing and cannot answer it.
    other = patient(client, "p-2")
    assert client.get("/v1/me/tests", headers=other).json() == []
    assert (
        client.post(f"/v1/me/tests/{assignment_id}", json=answers(), headers=other).status_code
        == 404
    )
    # Another practice's clinician cannot cancel it.
    theirs = key(client, GLOBEX, {"name": "dr.otro", "roles": ["reviewer"]})
    assert (
        client.delete(f"/v1/clinical/assignments/{assignment_id}", headers=theirs).status_code
        == 404
    )
    assert (
        client.delete(f"/v1/clinical/assignments/{assignment_id}", headers=dra).status_code == 204
    )
    assert (
        client.delete(f"/v1/clinical/assignments/{assignment_id}", headers=dra).status_code == 404
    )
    assert client.get("/v1/me/tests", headers=patient(client)).json() == []


def test_the_briefing_and_the_export_show_sent_tests(
    client: TestClient, dra: dict[str, str]
) -> None:
    body = {"professional_id": "dra.vera", "display_name": "Vera", "pack_id": "ec-psychologist"}
    client.post("/v1/admin/professionals", json={**body, "staff_id": "dra.vera"}, headers=ADMIN)
    send(client, dra, phq9(client, dra))
    briefing = client.get("/v1/clinical/patients/p-1/briefing", headers=dra).json()
    assert "Test enviado sin responder: PHQ-9" in briefing["text"]
    exported = client.get("/v1/subjects/p-1/export", headers=ADMIN).json()
    assert [a["status"] for a in exported["instrument_assignments"]] == ["pending"]
