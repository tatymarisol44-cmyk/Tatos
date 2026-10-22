"""Clinician-entered measurements are private, audited parts of the health record."""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from orchestrator.api.app import create_app
from orchestrator.catalog import Catalog
from orchestrator.config import Settings
from orchestrator.llm import FakeLLM
from orchestrator.service import Orchestrator

ADMIN = {"X-API-Key": "test-key"}


@pytest.fixture
def client(settings: Settings, catalog: Catalog) -> Iterator[TestClient]:
    settings.tenant_packs = {"acme": "ec-psychologist"}
    orch = Orchestrator(settings, catalog=catalog, llm=FakeLLM())
    with TestClient(create_app(settings, orch)) as c:
        c.post(
            "/v1/crm/patients",
            json={"id": "p-metric", "display_name": "Paciente sintética"},
            headers=ADMIN,
        )
        yield c


def staff(client: TestClient, name: str, role: str) -> dict[str, str]:
    key = client.post("/v1/admin/staff", json={"name": name, "roles": [role]}, headers=ADMIN)
    assert key.status_code == 201
    return {"X-API-Key": key.json()["key"]}


def test_clinician_records_and_reads_measurement_history(client: TestClient) -> None:
    clinician = staff(client, "dra.metricas", "reviewer")
    url = "/v1/clinical/patients/p-metric/metrics"
    old = client.post(
        url,
        json={
            "name": "Malestar",
            "value": 4,
            "unit": "sobre 10",
            "measured_on": "2026-10-01",
            "note": "Medición sintética.",
        },
        headers=clinician,
    )
    new = client.post(
        url,
        json={
            "name": "Malestar",
            "value": 2,
            "unit": "sobre 10",
            "measured_on": "2026-10-20",
        },
        headers=clinician,
    )

    assert old.status_code == new.status_code == 201
    assert new.json()["value"] == 2
    history = client.get(url, headers=clinician)
    assert history.status_code == 200
    assert [entry["measured_on"] for entry in history.json()] == ["2026-10-20", "2026-10-01"]
    assert history.json()[1]["note"] == "Medición sintética."


def test_only_clinicians_can_record_or_read_measurements(client: TestClient) -> None:
    reception = staff(client, "recepcion.metricas", "reception")
    url = "/v1/clinical/patients/p-metric/metrics"
    body = {
        "name": "Malestar",
        "value": 2,
        "unit": "sobre 10",
        "measured_on": "2026-10-20",
    }

    assert client.post(url, json=body, headers=reception).status_code == 403
    assert client.get(url, headers=reception).status_code == 403


def test_measurements_are_exported_and_retained_when_patient_is_restricted(
    client: TestClient,
) -> None:
    clinician = staff(client, "dra.exporta", "reviewer")
    privacy = staff(client, "privacidad.metricas", "privacy")
    url = "/v1/clinical/patients/p-metric/metrics"
    made = client.post(
        url,
        json={"name": "Sueño", "value": 7, "unit": "horas", "measured_on": "2026-10-20"},
        headers=clinician,
    )
    assert made.status_code == 201

    exported = client.get("/v1/subjects/p-metric/export", headers=privacy)
    assert exported.status_code == 200
    assert exported.json()["clinical_metrics"][0]["name"] == "Sueño"

    erased = client.delete("/v1/subjects/p-metric", headers=privacy)
    assert erased.status_code == 200
    assert erased.json()["clinical_metrics"] == "retained with the clinical record"
    assert client.get(url, headers=clinician).json()[0]["value"] == 7
    rejected = client.post(
        url,
        json={"name": "Sueño", "value": 6, "unit": "horas", "measured_on": "2026-10-21"},
        headers=clinician,
    )
    assert rejected.status_code == 422


def test_measurement_requires_existing_patient(client: TestClient) -> None:
    clinician = staff(client, "dra.404", "reviewer")
    response = client.get("/v1/clinical/patients/missing/metrics", headers=clinician)
    assert response.status_code == 404
