"""Deleting synthetic registrations without losing retained clinical records."""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from orchestrator.api.app import create_app
from orchestrator.catalog import Catalog
from orchestrator.config import Settings
from orchestrator.llm import FakeLLM
from orchestrator.service import Orchestrator

ADMIN = {"X-API-Key": "test-key"}
OTHER = {"X-API-Key": "other-key"}


@pytest.fixture
def client(settings: Settings, catalog: Catalog) -> Iterator[TestClient]:
    with TestClient(
        create_app(settings, Orchestrator(settings, catalog=catalog, llm=FakeLLM()))
    ) as c:
        yield c


def test_unused_patient_data_is_erased_and_active_lists_filter_before_limit(
    client: TestClient,
) -> None:
    original = {
        "id": "cleanup-demo",
        "display_name": "A Prueba Sintética",
        "phone": "0990000000",
        "email": "demo@example.test",
        "birth_date": "1990-01-01",
        "telegram_chat_id": "123456",
        "preferred_channel": "phone",
    }
    assert client.post("/v1/crm/patients", json=original, headers=ADMIN).status_code == 201
    assert client.post("/v1/crm/patients", json=original, headers=OTHER).status_code == 201
    assert (
        client.post(
            "/v1/crm/patients", json={"id": "keep", "display_name": "Z Conservada"}, headers=ADMIN
        ).status_code
        == 201
    )
    access = client.post("/v1/crm/patients/cleanup-demo/access", headers=ADMIN).json()["key"]
    assert client.delete("/v1/subjects/cleanup-demo", headers=ADMIN).status_code == 200
    erased = client.get("/v1/crm/patients/cleanup-demo", headers=ADMIN).json()
    assert erased["display_name"] == "Paciente eliminado" and erased["restricted"]
    assert all(
        erased[field] is None
        for field in ("phone", "email", "birth_date", "telegram_chat_id", "preferred_channel")
    )
    active = client.get("/v1/crm/patients?include_restricted=false&limit=1", headers=ADMIN).json()
    assert [p["id"] for p in active] == ["keep"]
    assert len(client.get("/v1/crm/patients?include_restricted=true", headers=ADMIN).json()) == 2
    untouched = client.get("/v1/crm/patients/cleanup-demo", headers=OTHER).json()
    assert untouched["display_name"] == original["display_name"] and not untouched["restricted"]
    assert client.get("/v1/me", headers={"X-API-Key": access}).status_code == 401


def test_identity_and_clinical_document_are_retained_after_erasure(client: TestClient) -> None:
    patient = {
        "id": "clinical-demo",
        "display_name": "Historia Sintética",
        "birth_date": "1990-01-01",
    }
    assert client.post("/v1/crm/patients", json=patient, headers=ADMIN).status_code == 201
    staff = client.post(
        "/v1/admin/staff", json={"name": "dra.cleanup", "roles": ["reviewer"]}, headers=ADMIN
    ).json()
    professional = {
        "professional_id": "dra.cleanup",
        "display_name": "Profesional Demo",
        "pack_id": "ec-psychologist",
        "staff_id": "dra.cleanup",
    }
    assert (
        client.post("/v1/admin/professionals", json=professional, headers=ADMIN).status_code == 201
    )
    clinician = {"X-API-Key": staff["key"]}
    path = "/v1/clinical/patients/clinical-demo/documents"
    made = client.post(
        path, json={"doc_type": "session_note", "body": "Nota sintética."}, headers=clinician
    )
    assert made.status_code == 201
    assert client.delete("/v1/subjects/clinical-demo", headers=ADMIN).status_code == 200
    retained = client.get("/v1/crm/patients/clinical-demo", headers=ADMIN).json()
    assert retained["display_name"] == patient["display_name"]
    assert retained["birth_date"] == patient["birth_date"] and retained["restricted"]
    assert (
        client.get(path, headers=clinician).json()[0]["document_id"] == made.json()["document_id"]
    )
    assert client.get("/v1/crm/patients?include_restricted=false", headers=ADMIN).json() == []


def test_reception_cannot_erase_a_patient(client: TestClient) -> None:
    client.post("/v1/crm/patients", json={"id": "demo", "display_name": "Prueba"}, headers=ADMIN)
    staff = client.post(
        "/v1/admin/staff", json={"name": "reception.demo", "roles": ["reception"]}, headers=ADMIN
    ).json()
    assert (
        client.delete("/v1/subjects/demo", headers={"X-API-Key": staff["key"]}).status_code == 403
    )
    assert not client.get("/v1/crm/patients/demo", headers=ADMIN).json()["restricted"]


def test_thread_deletion_refuses_active_work_and_is_audited(client: TestClient) -> None:
    made = client.post(
        "/v1/chat",
        json={"question": "Write a short greeting.", "thread_id": "cleanup-thread"},
        headers=ADMIN,
    )
    assert made.status_code == 200
    orch = client.app.state.orchestrator
    assert client.portal is not None
    client.portal.call(orch.leases.acquire, "acme", "cleanup-thread", "synthetic-holder")
    path = "/v1/threads/cleanup-thread"
    assert client.delete(path, headers=ADMIN).status_code == 409
    assert client.get(path, headers=ADMIN).status_code == 200
    client.portal.call(orch.leases.release, "acme", "cleanup-thread", "synthetic-holder")
    assert client.delete(path, headers=OTHER).status_code == 404
    assert client.delete(path, headers=ADMIN).status_code == 204
    assert client.get(path, headers=ADMIN).status_code == 404
    assert "thread.erased" in client.get("/v1/audit", headers=ADMIN).text
