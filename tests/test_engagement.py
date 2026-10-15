"""The engagement index (RF-MKT-01): three dimensions from behaviour, only with the
analytics consent, an absent dimension never counted as a zero, and the practice's
summary for the owner. Synthetic data only."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

import orchestrator.workshops as workshops_module
from orchestrator.api.app import create_app
from orchestrator.catalog import Catalog
from orchestrator.config import Settings
from orchestrator.db import utcnow
from orchestrator.engagement import level, overall, social_score
from orchestrator.llm import FakeLLM
from orchestrator.service import Orchestrator

ADMIN = {"X-API-Key": "test-key"}


def test_the_dimensions_and_the_level() -> None:
    assert social_score(attended=0, registered=0, with_company=False) is None  # nothing to read
    assert social_score(attended=0, registered=1, with_company=False) == 20
    assert social_score(attended=1, registered=1, with_company=True) == 80
    assert social_score(attended=5, registered=5, with_company=True) == 100
    assert overall({"atencion": None, "participacion": 60, "conexion": 80}) == 70
    assert overall({"atencion": None, "participacion": None, "conexion": None}) is None
    assert (level(70), level(69), level(39), level(None)) == ("alto", "medio", "bajo", None)


@pytest.fixture
def client(settings: Settings, catalog: Catalog) -> Iterator[TestClient]:
    orch = Orchestrator(settings, catalog=catalog, llm=FakeLLM())
    with TestClient(create_app(settings, orch)) as c:
        for pid in ("p-yes", "p-no"):
            c.post("/v1/crm/patients", json={"id": pid, "display_name": pid}, headers=ADMIN)
            for days_ago, status in ((40, "completed"), (20, "no_show"), (10, "completed")):
                visit = c.post(
                    "/v1/crm/appointments",
                    json={
                        "patient_id": pid,
                        "starts_at": (utcnow() - timedelta(days=days_ago)).isoformat(),
                    },
                    headers=ADMIN,
                ).json()
                c.post(
                    f"/v1/crm/appointments/{visit['id']}/status",
                    json={"status": status},
                    headers=ADMIN,
                )
        c.put(
            "/v1/subjects/p-yes/consents/analytics",
            json={"granted": True, "source": "app"},
            headers=ADMIN,
        )
        yield c


def test_only_with_the_analytics_consent(client: TestClient) -> None:
    refused = client.get("/v1/crm/patients/p-no/engagement", headers=ADMIN).json()
    assert refused == {"available": False, "reason": "sin consentimiento de analítica"}
    seen = client.get("/v1/crm/patients/p-yes/engagement", headers=ADMIN).json()
    assert seen["available"] and seen["dimensions"]["participacion"] == 67  # 2 of 3 visits
    assert seen["dimensions"]["atencion"] is None  # no message sent: absent, not zero
    assert seen["dimensions"]["conexion"] is None
    assert seen["score"] == 67 and seen["level"] == "medio"


def test_a_workshop_with_company_adds_social_connection(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    made = client.post(
        "/v1/workshops",
        json={
            "title": "Taller",
            "description": "Sintético.",
            "starts_at": (utcnow() + timedelta(days=2)).isoformat(),
            "place": "En el consultorio",
            "capacity": 10,
        },
        headers=ADMIN,
    ).json()
    key = client.post("/v1/crm/patients/p-yes/access", headers=ADMIN).json()["key"]
    client.post(
        f"/v1/me/workshops/{made['workshop_id']}",
        json={"companions": 1},
        headers={"X-API-Key": key},
    )
    later = utcnow() + timedelta(days=3)
    monkeypatch.setattr(workshops_module, "utcnow", lambda: later)
    client.post(
        f"/v1/workshops/{made['workshop_id']}/attendance",
        json={"patient_id": "p-yes", "attended": True},
        headers=ADMIN,
    )
    view = client.get("/v1/crm/patients/p-yes/engagement", headers=ADMIN).json()
    assert view["dimensions"]["conexion"] == 80
    assert view["score"] == 74 and view["level"] == "alto"  # mean of 67 and 80


def test_the_practice_summary_is_for_the_owner(client: TestClient) -> None:
    summary = client.get("/v1/insights/engagement", headers=ADMIN).json()
    assert summary["patients"] == 2 and summary["with_analytics_consent"] == 1
    assert summary["levels"]["medio"] == 1 and "no reemplaza" in summary["note"]
    reception = client.post(
        "/v1/admin/staff", json={"name": "r", "roles": ["reception"]}, headers=ADMIN
    ).json()
    headers = {"X-API-Key": reception["key"]}
    assert client.get("/v1/insights/engagement", headers=headers).status_code == 403
    assert client.get("/v1/crm/patients/p-yes/engagement", headers=headers).status_code == 200
