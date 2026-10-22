from __future__ import annotations

import secrets
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from tests.test_patient_app import SERVICE, _practice
from tests.test_patient_app import browser as browser
from tests.test_patient_app import client as client
from tests.test_patient_app import server as server


def credentials(email: str = "sol@example.test") -> dict[str, str]:
    return {"email": email, "password": secrets.token_urlsafe(24)}


def patient_key(client: TestClient, pid: str = "sol", headers: dict[str, str] = SERVICE) -> str:
    assert (
        client.post(
            "/v1/crm/patients",
            headers=headers,
            json={"id": pid, "display_name": "Paciente sintético"},
        ).status_code
        == 201
    )
    return str(client.post(f"/v1/crm/patients/{pid}/access", headers=headers).json()["key"])


def activate(client: TestClient, key: str, data: dict[str, str]) -> str:
    result = client.post("/v1/me/account", headers={"X-API-Key": key}, json=data)
    assert result.status_code == 200, result.text
    return str(result.json()["key"])


def test_activation_signin_signout_and_no_staff_access(client: TestClient) -> None:
    key = patient_key(client)
    data = credentials()
    session = activate(client, key, data)
    assert session.startswith("ps_")
    assert client.get("/v1/me", headers={"X-API-Key": key}).status_code == 401
    assert client.get("/v1/me", headers={"X-API-Key": session}).json()["profile"]["id"] == "sol"
    assert client.get("/v1/crm/patients", headers={"X-API-Key": session}).status_code == 403
    assert (
        client.post("/v1/me/account", headers={"X-API-Key": session}, json=data).status_code == 422
    )
    response = client.post("/v1/patient-auth/sign-in", json={**data, "email": " SOL@EXAMPLE.TEST "})
    assert response.status_code == 200
    second = response.json()["key"]
    assert (
        client.post("/v1/patient-auth/sign-out", headers={"X-API-Key": second}).status_code == 204
    )
    assert client.get("/v1/me", headers={"X-API-Key": second}).status_code == 401
    assert client.get("/v1/me", headers={"X-API-Key": session}).status_code == 200


def test_recovery_only_latest_link_revokes_old_sessions(client: TestClient) -> None:
    data = credentials()
    session = activate(client, patient_key(client), data)
    ordinary = client.post("/v1/crm/patients/sol/access", headers=SERVICE).json()["key"]
    assert (
        client.post("/v1/me/account", headers={"X-API-Key": ordinary}, json=data).status_code == 422
    )
    links = [
        client.post("/v1/crm/patients/sol/login-link", headers=SERVICE).json()["key"]
        for _ in range(2)
    ]
    changed = credentials()
    assert client.post(
        "/v1/me/account", headers={"X-API-Key": links[0]}, json=changed
    ).status_code in (401, 422)
    latest = activate(client, links[1], changed)
    assert client.get("/v1/me", headers={"X-API-Key": session}).status_code == 401
    assert client.post("/v1/patient-auth/sign-in", json=data).status_code == 401
    assert client.get("/v1/me", headers={"X-API-Key": latest}).status_code == 200
    assert client.post("/v1/patient-auth/sign-in", json=changed).status_code == 200


def test_wrong_email_and_password_same_response_and_lock(client: TestClient) -> None:
    data = credentials()
    activate(client, patient_key(client), data)
    unknown = client.post("/v1/patient-auth/sign-in", json=credentials("absent@example.test"))
    for _ in range(5):
        wrong = client.post("/v1/patient-auth/sign-in", json={**data, "password": "incorrect"})
        assert wrong.status_code == unknown.status_code == 401
        assert wrong.json() == unknown.json()
    assert client.post("/v1/patient-auth/sign-in", json=data).status_code == 401


def test_duplicate_email_does_not_link_another_patient_or_tenant(client: TestClient) -> None:
    data = credentials()
    first = activate(client, patient_key(client), data)
    other = patient_key(client, "other", {"X-API-Key": "other-key"})
    assert client.post("/v1/me/account", headers={"X-API-Key": other}, json=data).status_code == 422
    assert client.get("/v1/me", headers={"X-API-Key": first}).json()["profile"]["id"] == "sol"
    assert client.get("/v1/me", headers={"X-API-Key": other}).json()["profile"]["id"] == "other"
    assert (
        client.post("/v1/crm/patients/sol/login-link", headers={"X-API-Key": other}).status_code
        == 403
    )
    assert (
        client.post(
            "/v1/crm/patients/sol/login-link", headers={"X-API-Key": "other-key"}
        ).status_code
        == 404
    )


@pytest.mark.parametrize(
    "data",
    [
        {"email": "bad", "password": "long-enough-phrase"},
        {"email": "sol@example.test", "password": "123"},
    ],
)
def test_invalid_account_is_not_created(client: TestClient, data: dict[str, str]) -> None:
    key = patient_key(client)
    assert client.post("/v1/me/account", headers={"X-API-Key": key}, json=data).status_code == 422
    assert client.get("/v1/me/account", headers={"X-API-Key": key}).json()["can_activate"]


def test_patient_account_and_booking_in_mobile_browser(server: str, browser: Any) -> None:
    key = _practice(server)
    page = browser.new_page(viewport={"width": 390, "height": 844})
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(f"{server}/mi/#k={key}")
    for _ in range(3):
        page.get_by_text("Antes de empezar, una pregunta").wait_for()
        page.locator(".choice .answer").nth(1).click()
    page.get_by_text("Hola, Sol").wait_for()
    held: list[Any] = []
    page.route("**/v1/me/tests", lambda route: held.append(route), times=1)
    with page.expect_request(lambda request: request.url.endswith("/v1/me/tests")):
        page.evaluate("() => { refreshTestsBadge(); }")
    page.get_by_role("link", name="Mi correo y contraseña").click()
    data = credentials()
    page.get_by_label("Correo electrónico", exact=True).fill(data["email"])
    page.get_by_label("Contraseña nueva", exact=False).fill(data["password"])
    page.get_by_label("Repite la contraseña").fill(data["password"])
    page.get_by_role("button", name="Guardar correo y contraseña").click()
    page.get_by_text("Hola, Sol").wait_for()
    assert len(held) == 1
    with page.expect_response(lambda response: response.url.endswith("/v1/me/tests")):
        held[0].fulfill(status=401, json={})
    page.evaluate(
        "() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))"
    )
    assert page.get_by_text("Hola, Sol").is_visible()
    page.click("a[data-nav=perfil]")
    page.get_by_role("button", name="Salir de este celular", exact=True).click()
    page.get_by_role("button", name="Sí, salir de este celular", exact=True).click()
    page.get_by_label("Correo electrónico", exact=True).fill(data["email"])
    page.get_by_label("Contraseña", exact=True).fill(data["password"])
    page.get_by_role("button", name="Entrar", exact=True).click()
    page.get_by_text("Hola, Sol").wait_for()
    page.get_by_role("link", name="Reservar una cita", exact=True).click()
    page.locator(".time-btn").first.click()
    page.get_by_role("button", name="Reservar el", exact=False).click()
    page.get_by_text("Tu cita quedó reservada", exact=False).wait_for()
    visits = httpx.get(f"{server}/v1/crm/appointments?patient_id=p-sol", headers=SERVICE).json()
    assert len(visits) == 1
    assert not errors


def test_expired_session_and_erased_patient_cannot_sign_in(client: TestClient) -> None:
    from datetime import timedelta

    from sqlalchemy import select, update

    from orchestrator.db import utcnow
    from orchestrator.patient_login import patient_logins, patient_sessions

    data = credentials()
    session = activate(client, patient_key(client), data)
    service = client.app.state.orchestrator

    async def expire_and_check_storage() -> None:
        async with service.db.engine.begin() as conn:
            row = (await conn.execute(select(patient_logins))).mappings().one()
            assert row["password_hash"].startswith("scrypt$")
            assert data["password"] not in str(row)
            stored = (await conn.execute(select(patient_sessions))).mappings().one()
            assert stored["key_hash"] != session
            await conn.execute(
                update(patient_sessions).values(expires_at=utcnow() - timedelta(seconds=1))
            )

    client.portal.call(expire_and_check_storage)
    assert client.get("/v1/me", headers={"X-API-Key": session}).status_code == 401
    fresh = client.post("/v1/patient-auth/sign-in", json=data).json()["key"]
    client.portal.call(service.crm.erase_subject, "acme", "sol")
    assert client.get("/v1/me", headers={"X-API-Key": fresh}).status_code == 401
    assert client.post("/v1/patient-auth/sign-in", json=data).status_code == 401

    async def cleared() -> None:
        async with service.db.engine.connect() as conn:
            assert (await conn.execute(select(patient_logins))).first() is None
            assert (await conn.execute(select(patient_sessions))).first() is None

    client.portal.call(cleared)


def test_simultaneous_activation_creates_one_account(client: TestClient) -> None:
    from concurrent.futures import ThreadPoolExecutor

    key = patient_key(client)
    second = client.post("/v1/crm/patients/sol/access", headers=SERVICE).json()["key"]

    def attempt(pair: tuple[str, str]) -> int:
        token, email = pair
        return client.post(
            "/v1/me/account", headers={"X-API-Key": token}, json=credentials(email)
        ).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        codes = list(
            pool.map(attempt, [(key, "first@example.test"), (second, "second@example.test")])
        )
    assert codes.count(200) == 1
    assert all(code in (200, 401, 422) for code in codes)


def test_missing_hours_are_explained_and_can_be_refreshed(server: str, browser: Any) -> None:
    from tests.test_patient_app import _open_booking

    key = _practice(server)
    endpoint = f"{server}/v1/agenda/professionals/dra.demo/hours"
    assert httpx.put(endpoint, headers=SERVICE, json={"hours": []}).status_code == 200
    page = browser.new_page()
    _open_booking(page, server, key)
    page.get_by_text("todavía no ha publicado sus horarios", exact=False).wait_for()
    assert page.locator(".time-btn").count() == 0
    hours = [
        {"day": d, "hours": "08:00-20:00", "slot_minutes": 60}
        for d in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
    ]
    assert httpx.put(endpoint, headers=SERVICE, json={"hours": hours}).status_code == 200
    page.get_by_role("button", name="Actualizar horarios").click()
    page.locator(".time-btn").first.wait_for()
