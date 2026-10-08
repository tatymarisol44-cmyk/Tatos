"""The agenda: working hours, free slots in the practice's time zone, no double booking
(staff or patient), the professional's agenda and private calendar feeds."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from orchestrator.api.app import create_app
from orchestrator.catalog import Catalog
from orchestrator.config import Settings
from orchestrator.llm import FakeLLM
from orchestrator.service import Orchestrator

ADMIN = {"X-API-Key": "test-key"}
GLOBEX = {"X-API-Key": "other-key"}
EVERY_DAY = [
    {"day": d, "hours": "09:00-12:00", "slot_minutes": 60}
    for d in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
]


@pytest.fixture
def client(settings: Settings, catalog: Catalog) -> Iterator[TestClient]:
    orch = Orchestrator(settings, catalog=catalog, llm=FakeLLM())
    with TestClient(create_app(settings, orch)) as c:
        c.post(
            "/v1/admin/professionals",
            json={
                "professional_id": "dra.vera",
                "display_name": "Dra Vera",
                "pack_id": "ec-psychologist",
            },
            headers=ADMIN,
        )
        c.post("/v1/crm/patients", json={"id": "p-1", "display_name": "Ana Paredes"}, headers=ADMIN)
        c.post("/v1/crm/patients", json={"id": "p-2", "display_name": "Luis Mora"}, headers=ADMIN)
        hours = c.put(
            "/v1/agenda/professionals/dra.vera/hours", json={"hours": EVERY_DAY}, headers=ADMIN
        )
        assert hours.status_code == 200, hours.text
        yield c


def patient_key(client: TestClient, pid: str) -> dict[str, str]:
    return {"X-API-Key": client.post(f"/v1/crm/patients/{pid}/access", headers=ADMIN).json()["key"]}


def test_slots_are_local_hours_and_bookings_never_overlap(client: TestClient) -> None:
    slots = client.get(
        "/v1/agenda/slots", params={"professional_id": "dra.vera", "days": 3}, headers=ADMIN
    ).json()
    assert slots and all(s["local"][11:] in ("09:00", "10:00", "11:00") for s in slots)
    first = datetime.fromisoformat(slots[0]["starts_at"])
    assert first.utcoffset() == timedelta(0)  # stored and returned in UTC
    assert first.hour in (14, 15, 16)  # 09:00-11:00 in Guayaquil (UTC-5)

    booked = client.post(
        "/v1/crm/appointments",
        json={
            "patient_id": "p-1",
            "starts_at": slots[0]["starts_at"],
            "duration_min": 60,
            "professional_id": "dra.vera",
        },
        headers=ADMIN,
    )
    assert booked.status_code == 201, booked.text
    # 30 minutes later overlaps: refused. Another professional's agenda is not affected.
    clash = (first + timedelta(minutes=30)).isoformat()
    again = client.post(
        "/v1/crm/appointments",
        json={"patient_id": "p-2", "starts_at": clash, "professional_id": "dra.vera"},
        headers=ADMIN,
    )
    assert again.status_code == 409 and "already has a visit" in again.json()["detail"]
    after = client.get(
        "/v1/agenda/slots", params={"professional_id": "dra.vera", "days": 3}, headers=ADMIN
    ).json()
    assert slots[0]["starts_at"] not in {s["starts_at"] for s in after}

    view = client.get(
        "/v1/agenda", params={"professional_id": "dra.vera", "days": 3}, headers=ADMIN
    ).json()
    assert [(v["patient_name"], v["professional_id"]) for v in view] == [
        ("Ana Paredes", "dra.vera")
    ]


def test_a_patient_books_only_a_free_slot(client: TestClient) -> None:
    ana, luis = patient_key(client, "p-1"), patient_key(client, "p-2")
    slots = client.get("/v1/me/slots", params={"professional_id": "dra.vera"}, headers=ana).json()
    take = slots[1]["starts_at"]
    made = client.post(
        "/v1/me/appointments", json={"professional_id": "dra.vera", "starts_at": take}, headers=ana
    )
    assert made.status_code == 201 and made.json()["duration_min"] == 60
    # The same slot for someone else, or a time that is not a slot: refused.
    taken = client.post(
        "/v1/me/appointments", json={"professional_id": "dra.vera", "starts_at": take}, headers=luis
    )
    assert taken.status_code == 409
    odd = (datetime.fromisoformat(take) + timedelta(minutes=7)).isoformat()
    assert (
        client.post(
            "/v1/me/appointments",
            json={"professional_id": "dra.vera", "starts_at": odd},
            headers=luis,
        ).status_code
        == 409
    )
    upcoming = client.get("/v1/me/appointments", headers=ana).json()["upcoming"]
    assert [a["id"] for a in upcoming] == [made.json()["id"]]  # in the patient's own agenda


def test_calendar_feeds_are_signed_and_hold_no_names(client: TestClient) -> None:
    ana = patient_key(client, "p-1")
    slot = client.get("/v1/me/slots", params={"professional_id": "dra.vera"}, headers=ana).json()[0]
    client.post(
        "/v1/me/appointments",
        json={"professional_id": "dra.vera", "starts_at": slot["starts_at"]},
        headers=ana,
    )

    link = client.post("/v1/agenda/professionals/dra.vera/calendar-link", headers=ADMIN).json()[
        "url"
    ]
    path = link.split("localhost:8000", 1)[1]
    feed = client.get(path)
    assert feed.status_code == 200 and feed.headers["content-type"].startswith("text/calendar")
    assert "BEGIN:VEVENT" in feed.text and "Sesión · AP" in feed.text
    assert "Ana" not in feed.text and "Paredes" not in feed.text  # initials only

    mine = (
        client.get("/v1/me/calendar-link", headers=ana).json()["url"].split("localhost:8000", 1)[1]
    )
    assert "Cita en" in client.get(mine).text
    # A forged signature, or another patient's feed with Ana's signature: nothing.
    assert client.get(path[:-44] + "0" * 40 + ".ics").status_code == 404
    assert client.get(mine.replace("/p-1/", "/p-2/")).status_code == 404


def test_hours_are_validated_and_scoped(client: TestClient) -> None:
    overlap = [{"day": "mon", "hours": "09:00-12:00"}, {"day": "mon", "hours": "11:00-13:00"}]
    assert (
        client.put(
            "/v1/agenda/professionals/dra.vera/hours", json={"hours": overlap}, headers=ADMIN
        ).status_code
        == 422
    )
    backwards = [{"day": "mon", "hours": "12:00-09:00"}]
    assert (
        client.put(
            "/v1/agenda/professionals/dra.vera/hours", json={"hours": backwards}, headers=ADMIN
        ).status_code
        == 422
    )
    assert client.get("/v1/agenda/professionals/dra.vera/hours", headers=GLOBEX).status_code == 404
    assert (
        client.get(
            "/v1/agenda/slots", params={"professional_id": "dra.vera"}, headers=GLOBEX
        ).status_code
        == 404
    )
