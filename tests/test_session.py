"""`GET /v1/session`: who is signed in to the staff app, never the key itself."""

from __future__ import annotations

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
    settings.practice_display_name = "Consultorio Demo"
    with TestClient(
        create_app(settings, Orchestrator(settings, catalog=catalog, llm=FakeLLM()))
    ) as c:
        yield c


def test_a_clinician_sees_their_name_roles_and_professional(client: TestClient) -> None:
    staff = client.post(
        "/v1/admin/staff", json={"name": "dra.vera", "roles": ["reviewer", "owner"]}, headers=ADMIN
    ).json()
    vera = {"X-API-Key": staff["key"]}
    before = client.get("/v1/session", headers=vera).json()
    assert before == {
        "id": "dra.vera",
        "kind": "staff",
        "roles": ["owner", "reviewer"],
        "clinician": True,
        "practice": "Consultorio Demo",
        "timezone": "America/Guayaquil",
        "professional": None,
    }
    body = {
        "professional_id": "vera",
        "display_name": "Psic. Vera Demo",
        "pack_id": "ec-psychologist",
        "staff_id": "dra.vera",
    }
    assert client.post("/v1/admin/professionals", json=body, headers=ADMIN).status_code == 201
    after = client.get("/v1/session", headers=vera).json()
    assert after["professional"] == {
        "professional_id": "vera",
        "display_name": "Psic. Vera Demo",
        "pack_id": "ec-psychologist",
    }
    assert staff["key"] not in str(after)


def test_who_may_ask(client: TestClient) -> None:
    service = client.get("/v1/session", headers=ADMIN).json()
    assert service["kind"] == "service" and service["clinician"] is False  # need to know
    assert client.get("/v1/session").status_code == 401
    client.post("/v1/crm/patients", json={"id": "p-1", "display_name": "Ana"}, headers=ADMIN)
    patient = client.post("/v1/crm/patients/p-1/access", headers=ADMIN).json()
    assert client.get("/v1/session", headers={"X-API-Key": patient["key"]}).status_code == 403


def test_the_staff_app_is_served_under_the_strict_policy(client: TestClient) -> None:
    page = client.get("/app")
    assert page.status_code == 200 and 'lang="es"' in page.text
    policy = page.headers["Content-Security-Policy"]
    assert "script-src 'self'" in policy and "unsafe-inline" not in policy
    assert "<script>" not in page.text and " style=" not in page.text  # nothing inline
    for asset in ("/static/ui.css", "/static/consultorio.js"):
        assert client.get(asset).status_code == 200
