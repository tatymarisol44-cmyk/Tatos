"""The clinical record (ADR 0014): append-only entries typed by the author's pack, a
psychotherapy note visible to its author only, no access for admin keys or
reception, and every read audited without content. Synthetic data only."""

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

ADMIN = {"X-API-Key": "test-key"}  # tenant acme (pack `general`), an admin service key
GLOBEX = {"X-API-Key": "other-key"}
PATIENT = "p-001"
SECRET_NOTE = "Asociaciones libres sobre la figura paterna; transferencia intensa."


@pytest.fixture
def client(settings: Settings, catalog: Catalog) -> Iterator[TestClient]:
    orch = Orchestrator(settings, catalog=catalog, llm=FakeLLM())
    with TestClient(create_app(settings, orch)) as c:
        assert (
            c.post(
                "/v1/crm/patients",
                json={"id": PATIENT, "display_name": "Paciente Demo"},
                headers=ADMIN,
            ).status_code
            == 201
        )
        yield c


def clinician(client: TestClient, name: str, pack: str) -> dict[str, str]:
    staff = client.post(
        "/v1/admin/staff", json={"name": name, "roles": ["reviewer"]}, headers=ADMIN
    ).json()
    body = {"professional_id": name, "display_name": name, "pack_id": pack, "staff_id": name}
    assert client.post("/v1/admin/professionals", json=body, headers=ADMIN).status_code == 201
    return {"X-API-Key": staff["key"]}


def write(
    client: TestClient,
    headers: dict[str, str],
    doc_type: str,
    body: str = "Sesión 1.",
    **extra: Any,
) -> Any:
    payload = {"doc_type": doc_type, "body": body, **extra}
    return client.post(f"/v1/clinical/patients/{PATIENT}/documents", json=payload, headers=headers)


def listed(client: TestClient, headers: dict[str, str]) -> list[str]:
    response = client.get(f"/v1/clinical/patients/{PATIENT}/documents", headers=headers)
    assert response.status_code == 200, response.text
    return [d["doc_type"] for d in response.json()]


@pytest.fixture
def team(client: TestClient) -> tuple[dict[str, str], dict[str, str]]:
    return clinician(client, "dra.vera", "ec-psychologist"), clinician(
        client, "dr.ruiz", "ec-psychiatrist"
    )


def test_a_psychotherapy_note_is_for_its_author_only(
    client: TestClient, team: tuple[dict[str, str], dict[str, str]]
) -> None:
    vera, ruiz = team
    session = write(client, vera, "session_note")
    note = write(client, vera, "psychotherapy_note", SECRET_NOTE)
    assert session.status_code == 201 and note.status_code == 201
    assert note.json()["access"] == "author_only" and note.json()["pack_id"] == "ec-psychologist"
    note_id = note.json()["document_id"]

    assert listed(client, vera) == ["session_note", "psychotherapy_note"]
    assert listed(client, ruiz) == ["session_note"]  # another clinician: not even listed
    # An admin key opens no clinical data at all (need to know, test_need_to_know.py).
    assert (
        client.get(f"/v1/clinical/patients/{PATIENT}/documents", headers=ADMIN).status_code == 403
    )
    assert client.get(f"/v1/clinical/documents/{note_id}", headers=ruiz).status_code == 404
    assert client.get(f"/v1/clinical/documents/{note_id}", headers=ADMIN).status_code == 403
    assert (
        client.get(f"/v1/clinical/documents/{note_id}", headers=vera).json()["body"] == SECRET_NOTE
    )


def test_reception_has_no_access_to_the_record(
    client: TestClient, team: tuple[dict[str, str], dict[str, str]]
) -> None:
    vera, _ = team
    write(client, vera, "session_note")
    staff = client.post(
        "/v1/admin/staff", json={"name": "recepcion", "roles": ["reception"]}, headers=ADMIN
    ).json()
    reception = {"X-API-Key": staff["key"]}
    assert (
        client.get(f"/v1/clinical/patients/{PATIENT}/documents", headers=reception).status_code
        == 403
    )
    assert write(client, reception, "session_note").status_code == 403


@pytest.mark.parametrize(
    ("doc_type", "message"),
    [
        ("prescription", "has no document"),  # a psychologist's pack has no prescription
        ("controlled_worksheet", "has no document"),
        ("diary_entry", "written by the patient"),
        ("nope", "has no document"),
    ],
)
def test_types_must_exist_in_the_authors_pack(
    client: TestClient, team: tuple[dict[str, str], dict[str, str]], doc_type: str, message: str
) -> None:
    vera, _ = team
    refused = write(client, vera, doc_type)
    assert refused.status_code == 422 and message in refused.json()["detail"]


def test_the_psychiatrist_writes_with_his_own_pack(
    client: TestClient, team: tuple[dict[str, str], dict[str, str]]
) -> None:
    _, ruiz = team
    made = write(client, ruiz, "medication_followup", "Tolera bien la dosis actual.")
    assert made.status_code == 201 and made.json()["pack_id"] == "ec-psychiatrist"


def test_a_key_without_a_professional_uses_the_establishment_pack(client: TestClient) -> None:
    # A clinician who is not a registered professional writes with the establishment's
    # pack, here `general`, which has no clinical documents.
    staff = client.post(
        "/v1/admin/staff", json={"name": "suplente", "roles": ["reviewer"]}, headers=ADMIN
    ).json()
    assert write(client, {"X-API-Key": staff["key"]}, "session_note").status_code == 422


def test_unknown_patients_and_tenants(
    client: TestClient, team: tuple[dict[str, str], dict[str, str]]
) -> None:
    vera, _ = team
    payload = {"doc_type": "session_note", "body": "x"}
    unknown = client.post("/v1/clinical/patients/p-999/documents", json=payload, headers=vera)
    assert unknown.status_code == 422
    made = write(client, vera, "session_note").json()
    globex_doctor = client.post(
        "/v1/admin/staff", json={"name": "dr.globex", "roles": ["reviewer"]}, headers=GLOBEX
    ).json()
    for other_tenant in (GLOBEX, {"X-API-Key": globex_doctor["key"]}):
        response = client.get(f"/v1/clinical/documents/{made['document_id']}", headers=other_tenant)
        assert response.status_code in {403, 404}
        assert "Sesión" not in response.text


def test_corrections_amend_and_never_overwrite(
    client: TestClient, team: tuple[dict[str, str], dict[str, str]]
) -> None:
    vera, ruiz = team
    first = write(client, vera, "session_note", "Primera versión.").json()
    fixed = write(
        client, vera, "session_note", "Corrección: fecha errónea.", amends=first["document_id"]
    )
    assert fixed.status_code == 201 and fixed.json()["amends"] == first["document_id"]
    assert listed(client, vera) == ["session_note", "session_note"]  # both stay
    note = write(client, vera, "psychotherapy_note", SECRET_NOTE).json()
    # Another clinician cannot amend what they cannot see, and types must match.
    assert (
        write(client, ruiz, "psychotherapy_note", "x", amends=note["document_id"]).status_code
        == 422
    )
    assert (
        write(client, vera, "consent_therapy", "x", amends=first["document_id"]).status_code == 422
    )


def test_access_requests_get_the_record_but_not_psychotherapy_notes(
    client: TestClient, team: tuple[dict[str, str], dict[str, str]]
) -> None:
    vera, _ = team
    write(client, vera, "session_note", "Motivo de consulta: estrés laboral.")
    write(client, vera, "psychotherapy_note", SECRET_NOTE)
    exported = client.get(f"/v1/subjects/{PATIENT}/export", headers=ADMIN)
    assert exported.status_code == 200
    record = exported.json()["clinical_record"]
    assert [d["doc_type"] for d in record["documents"]] == ["session_note"]
    assert record["psychotherapy_notes_withheld"] == 1
    assert "transferencia" not in exported.text


def test_reads_are_audited_without_content(
    client: TestClient, team: tuple[dict[str, str], dict[str, str]]
) -> None:
    vera, _ = team
    note = write(client, vera, "psychotherapy_note", SECRET_NOTE).json()
    client.get(f"/v1/clinical/documents/{note['document_id']}", headers=vera)
    listed(client, vera)
    audit = client.get("/v1/audit", headers=ADMIN).text
    for action in (
        "clinical_document.written",
        "clinical_document.read",
        "clinical_document.listed",
    ):
        assert action in audit
    assert "transferencia" not in audit and "figura paterna" not in audit
