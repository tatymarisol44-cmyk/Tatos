"""Browser test of the staff app (/app): sign in with a personal key, complete the
profile and hours, book a visit on a free slot of the agenda, confirm it, and see it
on Hoy when it is today. No error and no policy violation on the page. Needs Playwright
and Chromium; skipped otherwise. Synthetic data only."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import socket
import sys
import threading
import time
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from orchestrator.api.app import create_app
from orchestrator.catalog import Catalog
from orchestrator.config import Settings
from orchestrator.llm import FakeLLM
from orchestrator.service import Orchestrator
from orchestrator.staff_login import totp_at, unb32

sync_api = pytest.importorskip("playwright.sync_api")

SERVICE = {"X-API-Key": "test-key"}
APP_SECRET = "app-secret-synthetic"
NUMBER_ID = "106540352242922"


@pytest.fixture
def server(settings: Settings, catalog: Catalog, tmp_path: Any) -> Iterator[str]:
    import uvicorn

    settings.media_dir = tmp_path / "media"  # rendered creatives stay out of the repository
    settings.tenant_packs = {"acme": "ec-psychologist"}
    settings.practice_display_name = "Consultorio Demo"
    settings.meta_app_secret = SecretStr(APP_SECRET)
    settings.whatsapp_verify_token = SecretStr("verify-synthetic")
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
def subprocess_loop() -> Iterator[None]:
    """Playwright's driver needs the Proactor loop on Windows (see test_console_e2e)."""
    if sys.platform != "win32":
        yield
        return
    previous = asyncio.get_event_loop_policy()
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    try:
        yield
    finally:
        asyncio.set_event_loop_policy(previous)


@pytest.fixture
def browser(subprocess_loop: None) -> Iterator[Any]:
    with sync_api.sync_playwright() as pw:
        try:
            chromium = pw.chromium.launch()
        except Exception as exc:  # no browser installed here
            pytest.skip(f"Chromium not available: {exc}")
        yield chromium
        chromium.close()


def _staff_key(base: str) -> str:
    resp = httpx.post(
        f"{base}/v1/admin/staff",
        json={"name": "dra.demo", "roles": ["admin", "owner", "reviewer"]},
        headers=SERVICE,
    )
    assert resp.status_code == 201, resp.text
    return str(resp.json()["key"])


def _use_key(page: Any) -> None:
    """E-mail sign-in is the default screen (ADR 0024); the key is behind its link."""
    page.wait_for_selector("#use-key:not([hidden])")
    page.click("#use-key")
    page.wait_for_selector("#signin-form:not([hidden])")


def _watch(page: Any) -> list[str]:
    """Script errors and console errors (a policy violation is one). The browser also logs
    every refused request ("Failed to load resource"): the app handles those on purpose."""
    problems: list[str] = []
    page.on("pageerror", lambda exc: problems.append(str(exc)))

    def console(msg: Any) -> None:
        if msg.type == "error" and not msg.text.startswith("Failed to load resource"):
            problems.append(msg.text)

    page.on("console", console)
    return problems


def test_sign_in_profile_hours_and_a_booking(server: str, browser: Any) -> None:
    key = _staff_key(server)
    httpx.post(
        f"{server}/v1/crm/patients",
        json={"id": "p-ana", "display_name": "Ana Sintética"},
        headers=SERVICE,
    )
    page = browser.new_page()
    problems = _watch(page)
    page.goto(f"{server}/app")

    # A wrong key is refused in Spanish; the right one opens the app.
    _use_key(page)
    page.fill("#signin-key", "sk_not-a-real-key")
    page.click("#signin-form button[type=submit]")
    page.wait_for_selector("#signin-status.error")
    assert "no es válida" in page.inner_text("#signin-status")
    page.fill("#signin-key", key)
    page.click("#signin-form button[type=submit]")
    page.wait_for_selector("#app:not([hidden])")
    assert page.inner_text("#me-name") == "dra.demo"
    assert "Consultorio Demo" in page.inner_text("#me-practice")
    page.wait_for_selector("text=Completa tu perfil")

    # Profile, then hours every day, so this week and the next have free slots.
    page.click("text=Completar ahora")
    page.fill("input[placeholder^='Ej.']", "Psic. Demo Sintética")
    page.click("text=Guardar mi perfil")
    page.wait_for_selector("text=Guardar horario")
    assert page.inner_text("#me-name") == "Psic. Demo Sintética"
    for box in page.query_selector_all(".hours-row > label:first-child input[type=checkbox]"):
        if not box.is_checked():
            box.check()
    page.click("text=Guardar horario")
    page.wait_for_selector("text=Horario guardado")

    # Next week always has free slots: book the first one for Ana and confirm it.
    page.click("a[data-nav=agenda]")
    page.wait_for_selector(".week-grid")
    page.click("button[aria-label='Semana siguiente']")
    page.wait_for_selector(".slot.f")
    page.click(".slot.f >> nth=0")
    page.select_option("#dialog select >> nth=0", "p-ana")
    page.click("#dialog button[type=submit]")
    page.wait_for_selector(".slot.w >> text=Ana Sintética")
    page.click(".slot.w >> text=Ana Sintética")
    page.get_by_role("button", name="Confirmar", exact=True).click()
    page.wait_for_selector(".slot.c >> text=Ana Sintética")

    visits = httpx.get(f"{server}/v1/agenda?days=21", headers=SERVICE).json()
    assert [(v["patient_id"], v["status"]) for v in visits] == [("p-ana", "confirmed")]
    assert visits[0]["kind"] == "Seguimiento · presencial"
    assert not problems, problems


def test_the_key_is_forgotten_on_sign_out(server: str, browser: Any) -> None:
    key = _staff_key(server)
    page = browser.new_page()
    problems = _watch(page)
    page.goto(f"{server}/app")
    _use_key(page)
    page.fill("#signin-key", key)
    page.check("#signin-remember")
    page.click("#signin-form button[type=submit]")
    page.wait_for_selector("#app:not([hidden])")
    page.reload()
    page.wait_for_selector("#app:not([hidden])")  # remembered on this computer
    page.click("#signout")
    page.wait_for_selector("#signin:not([hidden])")
    stored = page.evaluate("() => [sessionStorage.length, localStorage.length]")
    assert stored == [0, 0]
    page.reload()
    page.wait_for_selector("#signin:not([hidden])")
    assert not problems, problems


def _app_code(secret_text: str, ahead: int = 0) -> str:
    """What an authenticator app shows for this secret (`ahead` steps of 30 s later)."""
    secret = unb32(secret_text.replace(" ", ""))
    return totp_at(secret, int(time.time() // 30) + ahead)


def test_e_mail_sign_in_with_an_authenticator_and_the_team_card(server: str, browser: Any) -> None:
    made = httpx.post(
        f"{server}/v1/admin/team",
        json={"name": "Ana Recepción", "email": "ana@example.test", "roles": ["reception"]},
        headers=SERVICE,
    )
    assert made.status_code == 201, made.text
    token = made.json()["link"].split("#clave=")[1]
    page = browser.new_page()
    problems = _watch(page)

    # The personal link: a password first; its token leaves the address bar.
    page.goto(f"{server}/app#clave={token}")
    page.wait_for_selector("#password-form:not([hidden])")
    assert "clave" not in page.url
    page.fill("#new-password", "Mi gato duerme al sol")
    page.fill("#new-password-2", "Mi gato duerme al so")
    page.click("#password-form button[type=submit]")
    assert "no son iguales" in page.inner_text("#signin-status")
    page.fill("#new-password-2", "Mi gato duerme al sol")
    page.click("#password-form button[type=submit]")

    # Then the authenticator: a QR code, a link for the phone, the key to type by hand.
    page.wait_for_selector("#enroll-form:not([hidden])")
    assert page.locator("#enroll-qr svg path").count() == 1
    assert page.get_attribute("#enroll-open", "href").startswith("otpauth://totp/")
    secret = page.inner_text("#enroll-secret")
    page.fill("#enroll-code", "000000" if _app_code(secret) != "000000" else "111111")
    page.click("#enroll-form button[type=submit]")
    page.wait_for_selector("text=El código no es correcto")
    page.fill("#enroll-code", _app_code(secret))
    page.click("#enroll-form button[type=submit]")
    page.wait_for_selector("#app:not([hidden])")
    page.click("a[data-nav=ajustes]")
    page.wait_for_selector("text=Entras con tu correo ana@example.test")
    assert page.locator("text=Agregar a una persona").count() == 0  # reception: no team card
    page.click("#signout")
    page.wait_for_selector("#email-form:not([hidden])")
    assert page.evaluate("() => [sessionStorage.length, localStorage.length]") == [0, 0]

    # Signing in again: e-mail, password, then the app's next code.
    page.fill("#signin-email", "ana@example.test")
    page.fill("#signin-password", "Mi gato duerme al sol")
    page.click("#email-form button[type=submit]")
    page.wait_for_selector("#code-form:not([hidden])")
    page.fill("#signin-code", _app_code(secret, ahead=1))
    page.click("#code-form button[type=submit]")
    page.wait_for_selector("#app:not([hidden])")

    # The administration sees her as active, adds someone, gives Ana a new link, removes her.
    admin, _ = _signed_in(browser, server, _staff_key(server))
    admin.click("a[data-nav=ajustes]")
    row = ".doc:has-text('ana@example.test')"
    admin.wait_for_selector(f"{row} >> text=Activa")
    admin.click("text=Agregar a una persona")
    admin.fill("input[placeholder='Ej.: Ana Pérez']", "Luis Marketing")
    admin.fill("details input[type=email]", "luis@example.test")
    admin.check("details input[value=marketing]")
    admin.click("text=Invitar")
    admin.wait_for_selector("text=Envíale este enlace a Luis Marketing")
    assert "#clave=" in admin.input_value("input[aria-label='Enlace de acceso de Luis Marketing']")
    admin.click(f"{row} >> text=Enlace de acceso")
    admin.wait_for_selector("input[aria-label='Enlace de acceso de Ana Recepción']")
    admin.click(f"{row} >> text=Quitar acceso")
    admin.click(f"{row} >> text=Sí, quitar acceso")
    admin.wait_for_selector(row, state="detached")
    assert not problems, problems


def _signed_in(browser: Any, server: str, key: str) -> tuple[Any, list[str]]:
    page = browser.new_page()
    problems = _watch(page)
    page.goto(f"{server}/app")
    _use_key(page)
    page.fill("#signin-key", key)
    page.click("#signin-form button[type=submit]")
    page.wait_for_selector("#app:not([hidden])")
    return page, problems


def test_a_patient_record_from_creation_to_documents(server: str, browser: Any) -> None:
    key = _staff_key(server)
    body = {
        "professional_id": "dra.demo",
        "display_name": "Psic. Demo",
        "pack_id": "ec-psychologist",
        "staff_id": "dra.demo",
    }
    httpx.post(f"{server}/v1/admin/professionals", json=body, headers=SERVICE)
    page, problems = _signed_in(browser, server, key)

    # A new patient, from the list.
    page.click("a[data-nav=pacientes]")
    page.wait_for_selector("text=Aún no hay pacientes")
    page.click("text=Nuevo paciente >> nth=-1")
    page.fill("#view form input[type=text]", "Lucía Sintética")
    page.fill("#view form input[type=tel]", "0991112233")
    page.click("text=Crear paciente")
    page.wait_for_selector("h1:has-text('Lucía Sintética')")
    assert "0991112233" in page.inner_text("#view")
    assert "Nivel Bienvenida" in page.inner_text("#view")
    page.wait_for_selector("text=Borrador automático")  # the summary before a session
    assert "null" not in page.inner_text("#view")  # an absent element prints nothing

    # A session note in the record, then a correction that keeps the original.
    page.get_by_role("tab", name="Historia").click()
    page.wait_for_selector("text=Nueva entrada")
    page.fill("textarea", "Primera sesión: motivo de consulta, ansiedad sintética.")
    page.click("text=Guardar en la historia")
    page.wait_for_selector("article >> text=Primera sesión")
    page.click("article >> text=Corregir")
    page.fill("textarea", "Primera sesión: motivo de consulta, ansiedad leve sintética.")
    page.click("text=Guardar en la historia")
    page.wait_for_selector("article >> text=ansiedad leve")
    assert page.locator("article").count() == 2
    assert page.locator("article >> text=Corrección").count() == 1

    # A note typed and not saved: leaving the screen asks first, and "no" keeps the text.
    page.fill("textarea", "Nota a medio escribir")
    asked: list[str] = []
    page.once("dialog", lambda d: (asked.append(d.message), d.dismiss()))
    page.get_by_role("tab", name="Documentos").click()
    page.wait_for_timeout(300)
    assert asked and "sin guardar" in asked[0]
    assert page.input_value("textarea") == "Nota a medio escribir"

    # A document, uploaded and listed (this time leaving the unsaved note on purpose).
    page.once("dialog", lambda d: d.accept())
    page.get_by_role("tab", name="Documentos").click()
    page.wait_for_selector("text=Subir un documento")
    page.set_input_files(
        "input[type=file]",
        files=[
            {
                "name": "consentimiento.pdf",
                "mimeType": "application/pdf",
                "buffer": b"%PDF-1.7 sintetico",
            }
        ],
    )
    page.fill("input[placeholder^='Ej.']", "Consentimiento firmado")
    page.click("text=Subir documento")
    page.wait_for_selector(".doc >> text=Consentimiento firmado")

    # A test template still holds placeholder items: it cannot be sent yet.
    httpx.post(
        f"{server}/v1/clinical/instruments/from-template",
        json={"template": "phq9"},
        headers={"X-API-Key": key},
    )
    page.get_by_role("tab", name="Tests").click()
    page.wait_for_selector("text=Enviar un test a su app")
    page.select_option("#view form select", index=1)
    page.click("text=Enviar a su app")
    page.wait_for_selector("text=pega el texto oficial")
    assert not problems, problems


def test_a_slow_screen_never_paints_over_a_newer_one(server: str, browser: Any) -> None:
    """Hoy's data is held back while the person moves to Pacientes: when it finally
    arrives, it must not replace the screen she is on (found under load in CI)."""
    key = _staff_key(server)
    page = browser.new_page()
    problems = _watch(page)
    held: list[Any] = []
    page.route("**/v1/agenda?days=1", lambda route: held.append(route))
    page.goto(f"{server}/app")
    _use_key(page)
    page.fill("#signin-key", key)
    page.click("#signin-form button[type=submit]")
    page.wait_for_selector("#app:not([hidden])")
    page.wait_for_function("() => true")
    assert held, "Hoy asked for today's agenda"
    page.click("a[data-nav=pacientes]")
    page.wait_for_selector("text=Aún no hay pacientes")
    for route in held:
        route.continue_()
    page.wait_for_timeout(500)
    assert "Aún no hay pacientes" in page.inner_text("#view")
    assert "Citas de hoy" not in page.inner_text("#view")
    assert not problems, problems


def _whatsapp(server: str, text: str, sender: str = "15550001111") -> None:
    message = {"from": sender, "id": f"wamid.{len(text)}", "type": "text", "text": {"body": text}}
    value = {"metadata": {"phone_number_id": NUMBER_ID}, "messages": [message]}
    body = json.dumps(
        {
            "object": "whatsapp_business_account",
            "entry": [{"id": "WABA", "changes": [{"field": "messages", "value": value}]}],
        }
    ).encode()
    signature = "sha256=" + hmac.new(APP_SECRET.encode(), body, hashlib.sha256).hexdigest()
    headers = {"X-Hub-Signature-256": signature, "Content-Type": "application/json"}
    assert (
        httpx.post(f"{server}/v1/channels/meta", content=body, headers=headers).status_code == 200
    )


def test_the_inbox_draft_reply_link_and_close(server: str, browser: Any) -> None:
    key = _staff_key(server)
    account = {
        "network": "whatsapp",
        "external_id": NUMBER_ID,
        "handle": "+1 555 000 0000",
        "secret_ref": "WA_DEMO",
    }
    assert (
        httpx.post(f"{server}/v1/social/accounts", json=account, headers=SERVICE).status_code == 201
    )
    httpx.post(
        f"{server}/v1/crm/patients",
        json={"id": "p-sol", "display_name": "Sol Sintética"},
        headers=SERVICE,
    )
    _whatsapp(server, "Hola, ¿atienden los sábados por la mañana?")
    page, problems = _signed_in(browser, server, key)
    page.wait_for_selector("#badge-unread:not([hidden])")

    page.click("a[data-nav=mensajes]")
    page.wait_for_selector(".thread >> text=Persona sin vincular")
    page.click(".thread >> text=Persona sin vincular")
    page.wait_for_selector(".bubble.in >> text=atienden los sábados")

    # The AI suggests; nothing is sent until the person sends it.
    page.click("text=Sugerir respuesta con IA")
    page.wait_for_selector(".bubble.draft")
    page.click("text=Usar este borrador")
    assert "Gracias por escribirnos" in page.input_value("textarea")
    page.click("#view form >> text=Enviar")
    page.wait_for_selector("text=Guardado sin enviar")  # no credential here: a dry run

    # Linked to Sol's record, then marked as handled.
    page.wait_for_selector(".bubble.out >> text=No enviado")
    page.select_option("select[aria-label='Vincular a una paciente']", "p-sol")
    page.wait_for_selector(".thread >> text=Sol Sintética")
    page.click("text=Marcar como atendida")
    page.wait_for_selector(".thread >> text=Sol Sintética")
    page.wait_for_selector("#badge-unread", state="hidden")
    conversations = httpx.get(f"{server}/v1/inbox", headers={"X-API-Key": key}).json()
    assert [(c["patient_id"], c["status"]) for c in conversations] == [("p-sol", "closed")]
    assert not problems, problems


def test_tests_with_official_texts_and_the_on_call_list(server: str, browser: Any) -> None:
    key = _staff_key(server)
    httpx.post(
        f"{server}/v1/crm/patients",
        json={"id": "p-mar", "display_name": "Mar Sintética"},
        headers=SERVICE,
    )
    page, problems = _signed_in(browser, server, key)
    page.click("a[data-nav=ajustes]")
    page.wait_for_selector("text=Nadie está de guardia")

    # PHQ-9 from the template: not sendable until every item has its official text.
    page.click("text=Agregar PHQ-9 (depresión)")
    page.wait_for_selector("text=Guardar textos")
    page.click("text=Guardar textos")
    page.wait_for_selector("text=Faltan 9 preguntas")
    for n, area in enumerate(page.query_selector_all("#view form textarea")[1:], start=1):
        area.fill(f"Pregunta sintética número {n}")
    page.click("text=Guardar textos")
    page.wait_for_selector("text=Textos guardados")
    page.click("text=Volver a Ajustes")
    page.wait_for_selector("text=Lista para enviar")

    # Now it can be sent to a patient's app.
    page.goto(f"{server}/app#paciente/p-mar/tests")
    page.wait_for_selector("text=Enviar un test a su app")
    page.select_option("#view form select", index=1)
    page.click("text=Enviar a su app")
    page.wait_for_selector("text=Esperando respuesta >> nth=1")

    # Someone on call for crises.
    page.click("a[data-nav=ajustes]")
    page.wait_for_selector("text=Agregar a la guardia")
    page.fill("form:has-text('Agregar a la guardia') input[type=text]", "Psic. Demo de guardia")
    page.fill("form:has-text('Agregar a la guardia') input[type=tel]", "+593 99 000 0000")
    page.click("text=Agregar a la guardia")
    page.wait_for_selector("text=Psic. Demo de guardia")
    shown = page.inner_text(".doc:has-text('Psic. Demo de guardia')")
    assert "De guardia" in shown and "WhatsApp" in shown and "0000" in shown
    assert not problems, problems


def test_a_workshop_published_from_marketing(server: str, browser: Any) -> None:
    key = _staff_key(server)
    page, problems = _signed_in(browser, server, key)
    page.click("a[data-nav=marketing]")
    page.get_by_role("tab", name="Talleres").click()
    page.wait_for_selector("text=Nuevo taller o charla")
    page.fill("input[placeholder^='Ej.: Taller']", "Taller para dormir mejor")
    page.fill("textarea", "Hábitos de sueño y manejo del estrés (sintético).")
    page.click("text=Publicar en la app de las pacientes")
    page.wait_for_selector(".doc >> text=Taller para dormir mejor")
    assert "0 de 12 cupos" in page.inner_text("#view")
    page.click(".doc:has-text('Taller para dormir mejor') >> text=Inscritos")
    page.wait_for_selector("#dialog >> text=Todavía nadie se inscribió")
    assert not problems, problems


def test_the_monthly_report_and_a_smart_goal(server: str, browser: Any) -> None:
    key = _staff_key(server)
    httpx.post(
        f"{server}/v1/crm/patients", json={"id": "p-r", "display_name": "Rosa"}, headers=SERVICE
    )
    page, problems = _signed_in(browser, server, key)
    page.click("a[data-nav=marketing]")
    page.wait_for_selector("text=Objetivos SMART del mes")
    assert "Pacientes nuevos" in page.inner_text("#view")
    page.select_option("select[aria-label='Qué medir']", "nuevos_pacientes")
    page.fill("input[aria-label=Meta]", "2")
    page.fill("input[placeholder^='Por qué importa']", "Que más personas conozcan el consultorio")
    page.click("text=Agregar objetivo")
    page.wait_for_selector(".doc >> text=Pacientes nuevos: llegar a 2")
    goal = ".doc:has-text('Pacientes nuevos: llegar a 2')"
    assert "50\u00a0%" in page.inner_text(goal)  # the number and its % never split

    # Removing it asks once more on the same button.
    page.click(f"{goal} >> text=Quitar")
    page.wait_for_selector(f"{goal} >> text=Sí, quitar")
    page.click(f"{goal} >> text=Sí, quitar")
    page.wait_for_selector("text=Aún no hay objetivos para este mes")
    assert page.locator(goal).count() == 0
    page.click("text=Mes anterior")
    page.wait_for_selector("text=Aún no hay objetivos para este mes")
    assert not problems, problems


def test_a_campaign_rehearsed_and_a_publication_scheduled(server: str, browser: Any) -> None:
    key = _staff_key(server)
    account = {
        "network": "instagram",
        "external_id": "ig-178414",
        "handle": "@consultorio.demo",
        "secret_ref": "IG_DEMO",
    }
    assert (
        httpx.post(f"{server}/v1/social/accounts", json=account, headers=SERVICE).status_code == 201
    )
    page, problems = _signed_in(browser, server, key)

    # A campaign in rehearsal: approved by a person, sent, nothing delivered.
    page.goto(f"{server}/app#marketing/campanas")
    page.wait_for_selector("text=Nueva campaña")
    page.fill("input[placeholder^='Ej.: Invitación']", "Invitación sintética al taller")
    page.click("text=Crear campaña")
    page.wait_for_selector("article >> text=Invitación sintética al taller")
    assert "PRUEBA (no envía nada)" in page.inner_text("article")
    page.get_by_role("button", name="Aprobar", exact=True).click()
    page.wait_for_selector("article >> text=Aprobada")
    page.get_by_role("button", name="Enviar", exact=True).click()
    page.wait_for_selector("article >> text=Resultados")
    page.click("article >> text=Resultados")
    page.wait_for_selector("#dialog >> text=Fue una prueba")

    # A publication: the image made by code, scheduled for tomorrow, then approved.
    page.goto(f"{server}/app#marketing/publicaciones")
    page.wait_for_selector("text=Nueva publicación")
    page.fill("input[placeholder^='Ej.: Dormir']", "Dormir bien también es salud mental")
    page.fill("textarea[placeholder^='Una idea']", "Horario regular\nPantallas fuera de la cama")
    page.fill(
        "form:has-text('Texto de la publicación') textarea >> nth=1",
        "Tres hábitos para esta semana.",
    )
    page.click("text=Crear la imagen")
    page.wait_for_selector("article >> text=Por aprobar")
    page.get_by_role("button", name="Programar", exact=True).click()
    page.wait_for_selector("article >> text=Por aprobar")
    page.get_by_role("button", name="Aprobar", exact=True).click()
    page.wait_for_selector("article >> text=Aprobada")
    [publication] = httpx.get(f"{server}/v1/social/publications", headers=SERVICE).json()
    assert publication["status"] == "approved" and publication["scheduled_for"]
    assert not problems, problems


@pytest.mark.parametrize("stage", ["password", "enroll"])
def test_browser_back_leaves_account_setup(server: str, browser: Any, stage: str) -> None:
    """Back to #ajustes must leave setup even though it cleared the signed-in session."""
    page = browser.new_page()
    problems = _watch(page)
    page.goto(f"{server}/app#ajustes")
    page.wait_for_selector("#email-form:not([hidden])")
    page.evaluate("location.hash = '#clave=synthetic-access-token-123456'")
    page.wait_for_selector("#password-form:not([hidden])")
    if stage == "enroll":
        page.route(
            "**/v1/auth/link",
            lambda route: route.fulfill(
                json={
                    "challenge": "synthetic-enrollment",
                    "uri": "otpauth://totp/Test?secret=JBSWY3DPEHPK3PXP",
                    "qr": ["111", "101", "111"],
                    "secret": "JBSWY3DPEHPK3PXP",
                }
            ),
        )
        page.fill("#new-password", "Synthetic password 123")
        page.fill("#new-password-2", "Synthetic password 123")
        page.click("#password-form button[type=submit]")
        page.wait_for_selector("#enroll-form:not([hidden])")
    page.go_back()
    page.wait_for_selector("#email-form:not([hidden])")
    assert page.url.endswith("#ajustes")
    assert page.locator("#password-form").is_hidden()
    assert page.locator("#enroll-form").is_hidden()
    assert page.input_value("#new-password") == ""
    assert page.inner_text("#enroll-secret") == ""
    assert page.locator("#enroll-qr svg").count() == 0
    page.get_by_role("button", name="Entrar con llave de emergencia").click()
    page.wait_for_selector("#signin-form:not([hidden])")
    assert not problems, problems
    page.close()
