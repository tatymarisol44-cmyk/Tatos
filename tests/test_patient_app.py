"""The patient's app (/mi/, a PWA): served under the strict policy, installable, and the
whole path in a browser: the personal link, the consents asked once, booking a free slot,
and a test whose alert answer gives the emergency lines at once and alerts the team.
The browser part needs Playwright and Chromium; skipped otherwise. Synthetic data only."""

from __future__ import annotations

import asyncio
import socket
import sys
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from orchestrator.api.app import create_app
from orchestrator.catalog import Catalog
from orchestrator.config import Settings
from orchestrator.llm import FakeLLM
from orchestrator.service import Orchestrator

SERVICE = {"X-API-Key": "test-key"}


@pytest.fixture
def client(settings: Settings, catalog: Catalog) -> Iterator[TestClient]:
    settings.practice_display_name = "Consultorio Demo"
    orch = Orchestrator(settings, catalog=catalog, llm=FakeLLM())
    with TestClient(create_app(settings, orch)) as c:
        yield c


def test_the_app_is_served_installable_and_strict(client: TestClient) -> None:
    moved = client.get("/mi", follow_redirects=False)
    assert moved.status_code == 308 and moved.headers["location"] == "/mi/"
    page = client.get("/mi/")
    assert page.status_code == 200 and 'lang="es"' in page.text
    assert "unsafe-inline" not in page.headers["Content-Security-Policy"]
    assert "<script>" not in page.text and " style=" not in page.text
    manifest = client.get("/mi/manifest.webmanifest")
    assert manifest.headers["content-type"].startswith("application/manifest+json")
    data = manifest.json()
    assert data["name"] == "Consultorio Demo" and data["scope"] == "/mi/"
    assert data["display"] == "standalone" and {i["sizes"] for i in data["icons"]} == {
        "192x192",
        "512x512",
    }
    for icon in data["icons"]:
        assert client.get(icon["src"]).headers["content-type"] == "image/png"
    worker = client.get("/mi/sw.js")
    assert worker.headers["content-type"].startswith("text/javascript")
    assert "/v1/" not in worker.text.split("SHELL =")[1].split(";")[0]  # never caches the API


def test_the_practice_and_its_active_professionals_only(client: TestClient) -> None:
    for pid, name in (("ana", "Psic. Ana Demo"), ("luis", "Psic. Luis Demo")):
        body = {"professional_id": pid, "display_name": name, "pack_id": "ec-psychologist"}
        assert client.post("/v1/admin/professionals", json=body, headers=SERVICE).status_code == 201
    client.delete("/v1/admin/professionals/luis", headers=SERVICE)
    client.post("/v1/crm/patients", json={"id": "p-1", "display_name": "Sol"}, headers=SERVICE)
    key = client.post("/v1/crm/patients/p-1/access", headers=SERVICE).json()["key"]
    practice = client.get("/v1/me/practice", headers={"X-API-Key": key}).json()
    assert practice == {
        "practice": "Consultorio Demo",
        "timezone": "America/Guayaquil",
        "professionals": [{"professional_id": "ana", "display_name": "Psic. Ana Demo"}],
    }
    assert client.get("/v1/me/practice", headers=SERVICE).status_code == 403


# --- in a browser ------------------------------------------------------------------------


@pytest.fixture
def server(settings: Settings, catalog: Catalog) -> Iterator[str]:
    import uvicorn

    settings.tenant_packs = {"acme": "ec-psychologist"}
    settings.practice_display_name = "Consultorio Demo"
    app = create_app(settings, Orchestrator(settings, catalog=catalog, llm=FakeLLM()))
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    deadline = time.time() + 15
    while not srv.started and time.time() < deadline:
        time.sleep(0.05)
    assert srv.started, "server did not start"
    yield f"http://127.0.0.1:{port}"
    srv.should_exit = True
    thread.join(timeout=10)


@pytest.fixture
def browser() -> Iterator[Any]:
    sync_api = pytest.importorskip("playwright.sync_api")
    previous = asyncio.get_event_loop_policy()
    if sys.platform == "win32":  # Playwright's driver needs the Proactor loop
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    try:
        with sync_api.sync_playwright() as pw:
            try:
                chromium = pw.chromium.launch()
            except Exception as exc:
                pytest.skip(f"Chromium not available: {exc}")
            yield chromium
            chromium.close()
    finally:
        asyncio.set_event_loop_policy(previous)


def _practice(server: str) -> str:
    """A professional with hours every day, a patient, and a ready PHQ-9 sent to her."""
    staff = httpx.post(
        f"{server}/v1/admin/staff",
        json={"name": "dra.demo", "roles": ["admin", "reviewer"]},
        headers=SERVICE,
    ).json()
    dra = {"X-API-Key": staff["key"]}
    body = {
        "professional_id": "dra.demo",
        "display_name": "Psic. Demo",
        "pack_id": "ec-psychologist",
        "staff_id": "dra.demo",
    }
    httpx.post(f"{server}/v1/admin/professionals", json=body, headers=SERVICE)
    days = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
    hours = [{"day": d, "hours": "08:00-20:00", "slot_minutes": 60} for d in days]
    httpx.put(
        f"{server}/v1/agenda/professionals/dra.demo/hours", json={"hours": hours}, headers=SERVICE
    )
    httpx.post(
        f"{server}/v1/crm/patients",
        json={"id": "p-sol", "display_name": "Sol Sintética", "phone": "0991234567"},
        headers=SERVICE,
    )
    made = httpx.post(
        f"{server}/v1/clinical/instruments/from-template", json={"template": "phq9"}, headers=dra
    ).json()
    spec = made["spec"]
    for n, item in enumerate(spec["items"], 1):
        item["text"] = f"Pregunta sintética {n}"
    httpx.put(
        f"{server}/v1/clinical/instruments/{made['instrument_id']}",
        json={"spec": spec},
        headers=dra,
    )
    sent = httpx.post(
        f"{server}/v1/clinical/patients/p-sol/assignments",
        json={"instrument_id": made["instrument_id"]},
        headers=dra,
    )
    assert sent.status_code == 201, sent.text
    access = httpx.post(f"{server}/v1/crm/patients/p-sol/access", headers=SERVICE).json()
    return str(access["key"])


def test_the_whole_path_on_a_phone(server: str, browser: Any) -> None:
    key = _practice(server)
    page = browser.new_page(viewport={"width": 390, "height": 844})
    problems: list[str] = []
    page.on("pageerror", lambda exc: problems.append(str(exc)))
    page.on(
        "console",
        lambda msg: (
            problems.append(msg.text)
            if msg.type == "error" and not msg.text.startswith("Failed to load resource")
            else None
        ),
    )
    page.goto(f"{server}/mi/#k={key}")

    # The consents, once each, nothing pre-selected; the key left the address bar.
    for _ in range(3):
        page.wait_for_selector("text=Antes de empezar")
        assert key not in page.url
        page.click(".choice .answer >> nth=1")  # "No, gracias" weighs the same as yes
    page.wait_for_selector("text=Hola, Sol")
    assert "Consultorio Demo" in page.inner_text("#practice")
    page.wait_for_selector("text=Tienes un test por responder")

    # Booking the first free time.
    page.click("a[data-nav=citas]")
    page.wait_for_selector(".time-btn")
    page.click(".time-btn >> nth=0")
    page.click("text=Reservar el")
    page.wait_for_selector("text=Tu cita quedó reservada")
    visits = httpx.get(f"{server}/v1/crm/appointments?patient_id=p-sol", headers=SERVICE).json()
    assert len(visits) == 1 and visits[0]["status"] == "scheduled"

    # The test: an answer above zero on item 9 gives the lines at once and alerts the team.
    page.click("a[data-nav=tests]")
    page.click("text=Responder")
    page.wait_for_selector("text=Pregunta sintética 9")
    page.click("text=Enviar mis respuestas")
    page.wait_for_selector("text=Faltan 9 preguntas")
    for n in range(9):
        group = page.locator(".answers").nth(n)
        group.locator(".answer").nth(2 if n == 8 else 0).click()
    page.click("text=Enviar mis respuestas")
    page.wait_for_selector("text=¡Gracias!")
    assert "ECU 911" in page.inner_text(".alert")
    assert "puntaje" not in page.inner_text("#view").lower()  # never the score
    alerts = httpx.get(f"{server}/v1/social/alerts", headers=SERVICE).json()
    assert [(a["kind"], a["subject_id"]) for a in alerts] == [("crisis", "p-sol")]

    # Mi perfil, and leaving this phone.
    page.click("a[data-nav=perfil]")
    page.wait_for_selector("text=Lo que aceptas")
    assert page.locator("text=No aceptado").count() == 3
    assert "Bienvenida" in page.inner_text("#view")  # her tier and its real benefits
    assert "nunca de cuántas sesiones" in page.inner_text("#view")
    page.click("text=Salir de este celular")  # leaving asks once more on the same button
    assert page.locator("text=Saliste").count() == 0
    page.click("text=Sí, salir de este celular")
    page.wait_for_selector("text=Saliste")
    assert page.evaluate("() => localStorage.length") == 0
    assert not problems, problems


def test_without_a_link_or_with_a_revoked_one(server: str, browser: Any) -> None:
    page = browser.new_page()
    page.goto(f"{server}/mi/")
    page.wait_for_selector("text=abre el enlace personal")
    page.goto(f"{server}/mi/#k=pk_not-a-real-key")
    page.wait_for_selector("text=Tu enlace venció o fue reemplazado")
    assert page.evaluate("() => localStorage.length") == 0


def test_signing_up_for_a_workshop_with_a_companion(server: str, browser: Any) -> None:
    key = _practice(server)
    made = httpx.post(
        f"{server}/v1/workshops",
        json={
            "title": "Charla: manejar la ansiedad",
            "description": "Herramientas prácticas (sintético).",
            "starts_at": (datetime.now(UTC) + timedelta(days=10)).isoformat(),
            "place": "En el consultorio",
            "capacity": 8,
        },
        headers=SERVICE,
    ).json()
    page = browser.new_page(viewport={"width": 390, "height": 844})
    problems: list[str] = []
    page.on("pageerror", lambda exc: problems.append(str(exc)))
    page.goto(f"{server}/mi/#k={key}")
    for _ in range(3):
        page.wait_for_selector("text=Antes de empezar")
        page.click(".choice .answer >> nth=1")
    page.wait_for_selector("text=Próximo taller")
    page.click("a[data-nav=talleres]")
    page.wait_for_selector("text=Charla: manejar la ansiedad")
    page.select_option("select[aria-label=Acompañantes]", "1")
    page.click("text=Inscribirme")
    page.wait_for_selector("text=Estás inscrita o inscrito")
    signed = httpx.get(
        f"{server}/v1/workshops/{made['workshop_id']}/registrations", headers=SERVICE
    ).json()
    assert [(r["patient_id"], r["companions"]) for r in signed] == [("p-sol", 1)]
    page.wait_for_selector("text=Inscrita/o")
    assert "Quedan" not in page.inner_text(".card:has-text('manejar la ansiedad')")
    assert not problems, problems
