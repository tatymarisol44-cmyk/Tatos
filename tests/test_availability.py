"""One schedule source for staff/patient, date overrides, buffers and stale bookings."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

from fastapi.testclient import TestClient

from tests.test_agenda import ADMIN, EVERY_DAY, GLOBEX, patient_key
from tests.test_agenda import client as client

BASE = "/v1/agenda/professionals/dra.vera"


def settings(client: TestClient) -> dict:
    response = client.get(BASE + "/availability", headers=ADMIN)
    assert response.status_code == 200, response.text
    return response.json()


def tomorrow(client: TestClient) -> str:
    return (
        datetime.now(client.app.state.orchestrator.agenda.tz).date() + timedelta(days=1)
    ).isoformat()


def test_closed_dates_remove_public_slots_and_keep_confirmed_visits(client: TestClient) -> None:
    patient = patient_key(client, "p-1")
    day = tomorrow(client)
    query = {"professional_id": "dra.vera", "on_date": day, "days": 1}
    staff = client.get("/v1/agenda/slots", params=query, headers=ADMIN).json()
    assert staff == client.get("/v1/me/slots", params=query, headers=patient).json()
    visit = client.post(
        "/v1/me/appointments",
        headers=patient,
        json={
            "professional_id": "dra.vera",
            "starts_at": staff[0]["starts_at"],
        },
    )
    assert visit.status_code == 201, visit.text
    result = client.put(
        BASE + "/exceptions",
        headers=ADMIN,
        json={
            "first": day,
            "last": day,
            "blocks": [],
            "version": settings(client)["version"],
        },
    )
    assert result.status_code == 200, result.text
    assert client.get("/v1/me/slots", params=query, headers=patient).json() == []
    old = next(
        a
        for a in client.get("/v1/crm/appointments", headers=ADMIN).json()
        if a["id"] == visit.json()["id"]
    )
    assert old["status"] == "scheduled" and old["duration_min"] == 60
    assert (
        client.post(
            "/v1/me/appointments",
            headers=patient,
            json={
                "professional_id": "dra.vera",
                "starts_at": staff[1]["starts_at"],
            },
        ).status_code
        == 409
    )


def test_date_replacement_reset_and_optimistic_version(client: TestClient) -> None:
    day = tomorrow(client)
    old = settings(client)["version"]
    body = {
        "first": day,
        "last": day,
        "blocks": [
            {"hours": "14:00-16:00", "slot_minutes": 30, "break_minutes": 15},
        ],
        "version": old,
    }
    assert client.put(BASE + "/exceptions", headers=ADMIN, json=body).status_code == 200
    assert client.put(BASE + "/exceptions", headers=ADMIN, json=body).status_code == 409
    query = {"professional_id": "dra.vera", "on_date": day, "days": 1}
    slots = client.get("/v1/agenda/slots", params=query, headers=ADMIN).json()
    assert [s["local"][11:] for s in slots] == ["14:00", "14:45", "15:30"]
    body.update(blocks=None, version=settings(client)["version"])
    assert client.put(BASE + "/exceptions", headers=ADMIN, json=body).status_code == 200
    assert (
        client.get("/v1/agenda/slots", params=query, headers=ADMIN)
        .json()[0]["local"]
        .endswith("09:00")
    )


def test_service_duration_virtual_mode_and_buffer_share_the_same_source(client: TestClient) -> None:
    patient = patient_key(client, "p-1")
    policy = {
        "services": [
            {
                "id": "virtual",
                "name": "Sesión virtual",
                "minutes": 30,
                "break_minutes": 15,
                "mode": "virtual",
            }
        ]
    }
    response = client.put(
        BASE + "/availability",
        headers=ADMIN,
        json={"version": settings(client)["version"], "policy": policy},
    )
    assert response.status_code == 200, response.text
    day = tomorrow(client)
    query = {"professional_id": "dra.vera", "on_date": day, "days": 1, "service_id": "virtual"}
    slots = client.get("/v1/me/slots", params=query, headers=patient).json()
    assert slots == client.get("/v1/agenda/slots", params=query, headers=ADMIN).json()
    assert all(s["minutes"] == 30 and s["buffer_minutes"] == 15 for s in slots)
    booked = client.post(
        "/v1/me/appointments",
        headers=patient,
        json={
            "professional_id": "dra.vera",
            "starts_at": slots[0]["starts_at"],
            "service_id": "virtual",
        },
    )
    assert booked.status_code == 201, booked.text
    own = next(
        a
        for a in client.get("/v1/crm/appointments", headers=ADMIN).json()
        if a["id"] == booked.json()["id"]
    )
    assert own["buffer_minutes"] == 15 and own["consultation_mode"] == "virtual"
    inside_buffer = (
        datetime.fromisoformat(slots[0]["starts_at"]) + timedelta(minutes=30)
    ).isoformat()
    blocked = client.post(
        "/v1/crm/appointments",
        headers=ADMIN,
        json={
            "patient_id": "p-2",
            "professional_id": "dra.vera",
            "starts_at": inside_buffer,
            "duration_min": 15,
        },
    )
    assert blocked.status_code == 409, blocked.text


def test_two_patient_devices_cannot_reserve_the_same_slot(client: TestClient) -> None:
    one, two = patient_key(client, "p-1"), patient_key(client, "p-2")
    slot = client.get("/v1/me/slots", headers=one, params={"professional_id": "dra.vera"}).json()[0]

    def book(headers: dict) -> int:
        return client.post(
            "/v1/me/appointments",
            headers=headers,
            json={
                "professional_id": "dra.vera",
                "starts_at": slot["starts_at"],
            },
        ).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(book, [one, two])) == [201, 409]


def test_settings_and_exceptions_do_not_cross_practices(client: TestClient) -> None:
    assert client.get(BASE + "/availability", headers=GLOBEX).status_code == 404
    day = tomorrow(client)
    assert (
        client.put(
            BASE + "/exceptions",
            headers=GLOBEX,
            json={
                "first": day,
                "last": day,
                "blocks": [],
                "version": 1,
            },
        ).status_code
        == 404
    )
    assert client.get(BASE + "/availability", headers=patient_key(client, "p-1")).status_code == 403


def test_exact_slot_is_not_hidden_by_the_first_two_hundred_results(client: TestClient) -> None:
    dense = [{**b, "hours": "07:00-23:00", "slot_minutes": 10} for b in EVERY_DAY]
    assert client.put(BASE + "/hours", headers=ADMIN, json={"hours": dense}).status_code == 200
    target = datetime.now(client.app.state.orchestrator.agenda.tz) + timedelta(days=5)
    target = target.replace(hour=20, minute=0, second=0, microsecond=0)
    assert client.portal is not None
    assert (
        client.portal.call(
            client.app.state.orchestrator.agenda.is_free_slot, "acme", "dra.vera", target
        )
        == 10
    )


def test_reschedule_preserves_identity_price_and_frees_previous_slot(client: TestClient) -> None:
    patient, stranger = patient_key(client, "p-1"), patient_key(client, "p-2")
    day = tomorrow(client)
    query = {"professional_id": "dra.vera", "on_date": day, "days": 1}
    slots = client.get("/v1/me/slots", params=query, headers=patient).json()
    made = client.post(
        "/v1/me/appointments",
        headers=patient,
        json={
            "professional_id": "dra.vera",
            "starts_at": slots[0]["starts_at"],
        },
    ).json()
    path = f"/v1/me/appointments/{made['id']}/reschedule"
    body = {"starts_at": slots[1]["starts_at"], "schedule_version": slots[1]["schedule_version"]}
    assert client.post(path, headers=stranger, json=body).status_code == 404
    changed = client.post(path, headers=patient, json=body)
    assert changed.status_code == 200, changed.text
    assert changed.json()["id"] == made["id"]
    assert changed.json()["starts_at"] == slots[1]["starts_at"]
    free = client.get("/v1/me/slots", params=query, headers=patient).json()
    assert slots[0]["starts_at"] in {s["starts_at"] for s in free}
    assert slots[1]["starts_at"] not in {s["starts_at"] for s in free}
    staff_path = f"/v1/agenda/appointments/{made['id']}/reschedule"
    assert client.post(staff_path, headers=GLOBEX, json=body).status_code == 404
    assert (
        client.post(
            staff_path, headers=ADMIN, json={"starts_at": slots[2]["starts_at"]}
        ).status_code
        == 200
    )
    audit = client.get("/v1/audit", headers=ADMIN).json()
    assert sum(e["action"] == "crm.appointment.rescheduled" for e in audit) == 2


def test_changed_duration_refuses_a_stale_browser_selection(client: TestClient) -> None:
    patient = patient_key(client, "p-1")
    slot = client.get(
        "/v1/me/slots",
        headers=patient,
        params={"professional_id": "dra.vera", "on_date": tomorrow(client)},
    ).json()[0]
    shorter = [{**b, "slot_minutes": 30} for b in EVERY_DAY]
    assert client.put(BASE + "/hours", headers=ADMIN, json={"hours": shorter}).status_code == 200
    body = {
        "professional_id": "dra.vera",
        "starts_at": slot["starts_at"],
        "schedule_version": slot["schedule_version"],
    }
    assert client.post("/v1/me/appointments", headers=patient, json=body).status_code == 409
    current = client.get(
        "/v1/me/slots",
        headers=patient,
        params={"professional_id": "dra.vera", "on_date": tomorrow(client)},
    ).json()[0]
    assert current["minutes"] == 30
    body["schedule_version"] = current["schedule_version"]
    assert client.post("/v1/me/appointments", headers=patient, json=body).status_code == 201
