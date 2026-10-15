"""Loyalty tiers (RF-MKT-02): from time in care and workshops, never from sessions or
money; and every benefit enforced: the booking window and the earlier workshop sign-up.
Synthetic data only."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from orchestrator.api.app import create_app
from orchestrator.catalog import Catalog
from orchestrator.config import Settings
from orchestrator.db import utcnow
from orchestrator.llm import FakeLLM
from orchestrator.loyalty import months_between, tier_for
from orchestrator.service import Orchestrator

ADMIN = {"X-API-Key": "test-key"}


@pytest.mark.parametrize(
    ("months", "workshops", "tier"),
    [
        (0, 0, "bienvenida"),
        (2, 0, "bienvenida"),
        (3, 0, "constancia"),
        (0, 1, "constancia"),
        (6, 0, "comunidad"),
        (1, 2, "comunidad"),
    ],
)
def test_the_rule_counts_time_and_community_not_sessions(
    months: int, workshops: int, tier: str
) -> None:
    assert tier_for(months, workshops).key == tier


def test_months_between_counts_whole_months() -> None:
    start = utcnow().replace(day=15)
    assert months_between(start, start + timedelta(days=10)) == 0
    assert months_between(start.replace(month=1), start.replace(month=7)) == 6


@pytest.fixture
def client(settings: Settings, catalog: Catalog) -> Iterator[TestClient]:
    orch = Orchestrator(settings, catalog=catalog, llm=FakeLLM())
    with TestClient(create_app(settings, orch)) as c:
        body = {"professional_id": "dra", "display_name": "Dra. Demo", "pack_id": "ec-psychologist"}
        c.post("/v1/admin/professionals", json=body, headers=ADMIN)
        days = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
        # One slot a day: the window is measured in days, not cut by the slot list's cap.
        hours = [{"day": d, "hours": "09:00-10:00", "slot_minutes": 60} for d in days]
        c.put("/v1/agenda/professionals/dra/hours", json={"hours": hours}, headers=ADMIN)
        for pid, months_ago in (("p-new", None), ("p-old", 7)):
            c.post("/v1/crm/patients", json={"id": pid, "display_name": pid}, headers=ADMIN)
            if months_ago:
                visit = c.post(
                    "/v1/crm/appointments",
                    json={
                        "patient_id": pid,
                        "starts_at": (utcnow() - timedelta(days=30 * months_ago)).isoformat(),
                    },
                    headers=ADMIN,
                ).json()
                c.post(
                    f"/v1/crm/appointments/{visit['id']}/status",
                    json={"status": "completed"},
                    headers=ADMIN,
                )
        yield c


def key(client: TestClient, pid: str) -> dict[str, str]:
    return {"X-API-Key": client.post(f"/v1/crm/patients/{pid}/access", headers=ADMIN).json()["key"]}


def test_each_patient_sees_their_tier_and_its_benefits(client: TestClient) -> None:
    new = client.get("/v1/me/loyalty", headers=key(client, "p-new")).json()["tier"]
    assert new["tier"] == "bienvenida" and new["next"]["name"] == "Constancia"
    old = client.get("/v1/me/loyalty", headers=key(client, "p-old")).json()["tier"]
    assert old["tier"] == "comunidad" and old["months_in_care"] >= 6 and old["next"] is None
    assert all("descuento" not in b.lower() for b in old["benefits"])
    staff = client.get("/v1/crm/patients/p-old/loyalty", headers=ADMIN).json()
    assert staff["tier"] == "comunidad"
    assert client.get("/v1/crm/patients/p-404/loyalty", headers=ADMIN).status_code == 404


def test_the_booking_window_follows_the_tier(client: TestClient) -> None:
    def furthest(pid: str) -> int:
        slots = client.get(
            "/v1/me/slots?professional_id=dra&days=60", headers=key(client, pid)
        ).json()
        last = max(s["starts_at"] for s in slots)
        return (utcnow() - utcnow().fromisoformat(last)).days * -1

    assert furthest("p-new") <= 21  # Bienvenida: 3 weeks
    assert 30 < furthest("p-old") <= 45  # Comunidad: 45 days


def test_higher_tiers_sign_up_for_workshops_first(client: TestClient) -> None:
    made = client.post(
        "/v1/workshops",
        json={
            "title": "Taller sintético",
            "description": "Para probar la inscripción anticipada.",
            "starts_at": (utcnow() + timedelta(days=10)).isoformat(),
            "place": "En el consultorio",
            "capacity": 10,
            "opens_at": (utcnow() + timedelta(days=3)).isoformat(),  # for everyone, in 3 days
        },
        headers=ADMIN,
    ).json()
    url = f"/v1/me/workshops/{made['workshop_id']}"
    early = client.post(url, json={"companions": 0}, headers=key(client, "p-new"))
    assert early.status_code == 409 and "opens at" in early.text
    assert client.post(url, json={"companions": 0}, headers=key(client, "p-old")).status_code == 200
    [shown] = client.get("/v1/me/workshops", headers=key(client, "p-new")).json()
    assert shown["opens_for_you"] > utcnow().isoformat()
    refused = client.post(
        "/v1/workshops",
        json={
            "title": "Al revés",
            "description": "Abre después de empezar.",
            "starts_at": (utcnow() + timedelta(days=2)).isoformat(),
            "place": "En línea",
            "capacity": 5,
            "opens_at": (utcnow() + timedelta(days=3)).isoformat(),
        },
        headers=ADMIN,
    )
    assert refused.status_code == 422
