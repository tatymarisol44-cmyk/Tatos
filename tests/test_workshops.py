"""Workshops and talks (RF-MKT-05): created by the practice, signed up for from the
patient's app with companions, seats never exceeded, attendance after the event, and the
patient's rights. Synthetic data only."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

import orchestrator.workshops as workshops_module
from orchestrator.api.app import create_app
from orchestrator.catalog import Catalog
from orchestrator.config import Settings
from orchestrator.db import utcnow
from orchestrator.llm import FakeLLM
from orchestrator.service import Orchestrator

ADMIN = {"X-API-Key": "test-key"}
GLOBEX = {"X-API-Key": "other-key"}


@pytest.fixture
def client(settings: Settings, catalog: Catalog) -> Iterator[TestClient]:
    orch = Orchestrator(settings, catalog=catalog, llm=FakeLLM())
    with TestClient(create_app(settings, orch)) as c:
        for pid in ("p-a", "p-b", "p-c"):
            c.post(
                "/v1/crm/patients",
                json={"id": pid, "display_name": f"Paciente {pid}"},
                headers=ADMIN,
            )
        yield c


def patient(client: TestClient, pid: str) -> dict[str, str]:
    return {"X-API-Key": client.post(f"/v1/crm/patients/{pid}/access", headers=ADMIN).json()["key"]}


def workshop(client: TestClient, capacity: int = 3, headers: dict[str, str] = ADMIN) -> Any:
    body = {
        "title": "Taller: dormir mejor",
        "description": "Hábitos de sueño y manejo del estrés (sintético).",
        "starts_at": (utcnow() + timedelta(days=5)).isoformat(),
        "duration_min": 90,
        "place": "En el consultorio",
        "capacity": capacity,
    }
    return client.post("/v1/workshops", json=body, headers=headers)


def test_seats_count_companions_and_are_never_exceeded(client: TestClient) -> None:
    made = workshop(client, capacity=3).json()
    wid = made["workshop_id"]
    a, b, c = patient(client, "p-a"), patient(client, "p-b"), patient(client, "p-c")
    [listed] = client.get("/v1/me/workshops", headers=a).json()
    assert listed["seats_left"] == 3 and listed["registered"] is False

    assert (
        client.post(f"/v1/me/workshops/{wid}", json={"companions": 1}, headers=a).json()[
            "seats_left"
        ]
        == 1
    )
    # Changing one's own companions does not count the patient twice.
    assert (
        client.post(f"/v1/me/workshops/{wid}", json={"companions": 0}, headers=a).json()[
            "seats_left"
        ]
        == 2
    )
    assert (
        client.post(f"/v1/me/workshops/{wid}", json={"companions": 1}, headers=b).status_code == 200
    )
    full = client.post(f"/v1/me/workshops/{wid}", json={"companions": 0}, headers=c)
    assert full.status_code == 409 and "seats" in full.text
    assert (
        client.post(f"/v1/me/workshops/{wid}", json={"companions": 4}, headers=c).status_code == 422
    )

    # Withdrawing frees the seats; the patient sees their own sign-up only.
    assert client.delete(f"/v1/me/workshops/{wid}", headers=b).status_code == 204
    assert client.delete(f"/v1/me/workshops/{wid}", headers=b).status_code == 404
    assert (
        client.post(f"/v1/me/workshops/{wid}", json={"companions": 0}, headers=c).status_code == 200
    )
    [mine] = client.get("/v1/me/workshops", headers=a).json()
    assert mine["registered"] is True and mine["companions"] == 0 and mine["seats_left"] == 1


def test_who_organises_and_who_sees_the_sign_ups(client: TestClient) -> None:
    reception = {
        "X-API-Key": client.post(
            "/v1/admin/staff", json={"name": "r", "roles": ["reception"]}, headers=ADMIN
        ).json()["key"]
    }
    assert workshop(client, headers=reception).status_code == 403
    past = workshop(client).json()  # a valid one, then one in the past
    body = {
        "title": "Charla",
        "description": "Sintética.",
        "starts_at": (utcnow() - timedelta(hours=1)).isoformat(),
        "place": "En línea",
        "capacity": 10,
    }
    assert client.post("/v1/workshops", json=body, headers=ADMIN).status_code == 422
    wid = past["workshop_id"]
    client.post(f"/v1/me/workshops/{wid}", json={"companions": 2}, headers=patient(client, "p-a"))
    signed = client.get(f"/v1/workshops/{wid}/registrations", headers=reception).json()
    assert [(r["name"], r["companions"]) for r in signed] == [("Paciente p-a", 2)]
    assert "workshop.registrations_read" in client.get("/v1/audit", headers=ADMIN).text
    # Another practice sees nothing of it.
    assert client.get("/v1/workshops", headers=GLOBEX).json() == []
    assert client.get(f"/v1/workshops/{wid}/registrations", headers=GLOBEX).status_code == 404


def test_cancelled_and_attendance_after_the_event(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    wid = workshop(client).json()["workshop_id"]
    a = patient(client, "p-a")
    client.post(f"/v1/me/workshops/{wid}", json={"companions": 0}, headers=a)
    early = client.post(
        f"/v1/workshops/{wid}/attendance",
        json={"patient_id": "p-a", "attended": True},
        headers=ADMIN,
    )
    assert early.status_code == 409  # not before it starts
    later = utcnow() + timedelta(days=6)
    monkeypatch.setattr(workshops_module, "utcnow", lambda: later)
    assert (
        client.post(
            f"/v1/workshops/{wid}/attendance",
            json={"patient_id": "p-a", "attended": True},
            headers=ADMIN,
        ).status_code
        == 204
    )
    assert (
        client.post(
            f"/v1/workshops/{wid}/attendance",
            json={"patient_id": "p-b", "attended": True},
            headers=ADMIN,
        ).status_code
        == 404
    )  # never signed up
    monkeypatch.undo()

    other = workshop(client).json()["workshop_id"]
    assert client.post(f"/v1/workshops/{other}/cancel", headers=ADMIN).status_code == 204
    assert other not in [w["workshop_id"] for w in client.get("/v1/me/workshops", headers=a).json()]
    refused = client.post(f"/v1/me/workshops/{other}", json={"companions": 0}, headers=a)
    assert refused.status_code == 409


def test_the_patients_rights_include_their_sign_ups(client: TestClient) -> None:
    wid = workshop(client).json()["workshop_id"]
    client.post(f"/v1/me/workshops/{wid}", json={"companions": 1}, headers=patient(client, "p-a"))
    exported = client.get("/v1/subjects/p-a/export", headers=ADMIN).json()
    assert [(w["workshop"], w["companions"]) for w in exported["workshops"]] == [
        ("Taller: dormir mejor", 1)
    ]
    erased = client.delete("/v1/subjects/p-a", headers=ADMIN).json()
    assert erased["workshop_registrations"] == 1
    assert client.get(f"/v1/workshops/{wid}/registrations", headers=ADMIN).json() == []
