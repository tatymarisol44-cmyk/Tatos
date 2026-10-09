"""Need to know (LOPDP Art. 10.e, decision P9): patient clinical data is read only by
people who hold the clinician role (`reviewer`) themselves. Administering the practice
(admin keys, service keys) does not open the record; it still manages keys, documents,
reviews and data-subject requests. Synthetic data only."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from orchestrator.api.app import create_app
from orchestrator.auth import Principal, Role
from orchestrator.catalog import Catalog
from orchestrator.config import Settings
from orchestrator.llm import FakeLLM
from orchestrator.service import Orchestrator

SERVICE = {"X-API-Key": "test-key"}  # tenant acme, a service key (admin)
PATIENT = "p-1"
CLINICAL_READS = [
    f"/v1/clinical/patients/{PATIENT}/documents",
    f"/v1/clinical/patients/{PATIENT}/files",
    f"/v1/clinical/patients/{PATIENT}/follow-up",
    f"/v1/clinical/patients/{PATIENT}/instrument-results",
    "/v1/clinical/follow-up",
]


@pytest.fixture
def client(settings: Settings, catalog: Catalog) -> Iterator[TestClient]:
    orch = Orchestrator(settings, catalog=catalog, llm=FakeLLM())
    with TestClient(create_app(settings, orch)) as c:
        body = {"id": PATIENT, "display_name": "Ana"}
        assert c.post("/v1/crm/patients", json=body, headers=SERVICE).status_code == 201
        yield c


def staff(client: TestClient, name: str, *roles: str) -> dict[str, str]:
    made = client.post(
        "/v1/admin/staff", json={"name": name, "roles": list(roles)}, headers=SERVICE
    )
    assert made.status_code == 201, made.text
    return {"X-API-Key": made.json()["key"]}


def test_administering_the_practice_does_not_open_the_clinical_record(
    client: TestClient,
) -> None:
    admin = staff(client, "gerente", "admin")
    for path in CLINICAL_READS:
        for who in (SERVICE, admin):
            response = client.get(path, headers=who)
            assert response.status_code == 403, (path, response.status_code)
            assert "clinician" in response.json()["detail"]


def test_a_clinician_reads_it_even_when_also_admin(client: TestClient) -> None:
    for name, roles in (("dra.vera", ("reviewer",)), ("dr.ruiz", ("admin", "reviewer"))):
        who = staff(client, name, *roles)
        for path in CLINICAL_READS:
            assert client.get(path, headers=who).status_code == 200, (name, path)


def test_admin_keeps_the_administration_it_needs(client: TestClient) -> None:
    admin = staff(client, "gerente", "admin")
    assert client.get("/v1/audit", headers=admin).status_code == 200
    assert client.get("/v1/reviews", headers=admin).status_code == 200
    assert client.get(f"/v1/subjects/{PATIENT}/export", headers=admin).status_code == 200
    assert client.get("/v1/clinical/instrument-templates", headers=admin).status_code == 200


def test_the_clinician_check_is_explicit() -> None:
    def principal(*roles: str, kind: str = "staff") -> Principal:
        return Principal("acme", "x", kind, frozenset(roles))

    assert principal(Role.REVIEWER).is_clinician
    assert not principal(Role.ADMIN).is_clinician
    assert not principal(Role.ADMIN, kind="service").is_clinician
    assert not principal(Role.REVIEWER, kind="patient").is_clinician
    assert principal(Role.ADMIN).has(Role.REVIEWER)  # everything else is unchanged
