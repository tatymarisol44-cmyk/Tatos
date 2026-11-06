"""New survey/import/fiscal browser journeys, awaiting reviewed native execution."""

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx

from orchestrator.config import Settings
from orchestrator.db import utcnow
from tests.test_consultorio_e2e import SERVICE, _signed_in, _staff_key, _watch
from tests.test_consultorio_e2e import browser as browser
from tests.test_consultorio_e2e import server as server
from tests.test_consultorio_e2e import subprocess_loop as subprocess_loop
from tests.test_instrument_imports import MANIFEST
from tests.test_instruments import CUSTOM
from tests.test_tax_trials import PROFILE


def test_source_upload_review_and_existing_test_editor(
    server: str, browser: Any, tmp_path: Path
) -> None:
    key = _staff_key(server)
    page, problems = _signed_in(browser, server, key)
    page.goto(server + "/app#ajustes/importaciones")
    labels = {
        "name": "Nombre del test",
        "publisher": "Propietario o editor",
        "licence": "Licencia",
        "edition": "Edición",
        "source_reference": "Fuente o referencia bibliográfica",
        "permitted_use": "Uso permitido por la licencia",
    }
    for field, label in labels.items():
        page.get_by_label(label, exact=True).fill(MANIFEST[field])
    page.get_by_label("Archivo original, hasta 5 MB", exact=True).set_input_files(
        {
            "name": "synthetic.json",
            "mimeType": "application/json",
            "buffer": json.dumps(CUSTOM).encode(),
        }
    )
    page.get_by_role("button", name="Cargar como borrador", exact=True).click()
    page.get_by_label("Definición JSON revisada", exact=True).wait_for()
    page.get_by_label("Referencia autorizada de cálculo", exact=True).fill(
        "Aritmética sintética QA"
    )
    page.get_by_label("Referencia de validación clínica", exact=True).fill(
        "Solo prueba, sin validación clínica"
    )
    for label in (
        "Tengo derecho a usar esta edición y su puntuación.",
        "Revisé preguntas, cálculo, rangos y validación con el manual autorizado.",
        "La definición no contiene respuestas ni datos personales de pacientes.",
    ):
        page.get_by_role("checkbox", name=label, exact=True).check()
    page.get_by_role("button", name="Incorporar test privado tras revisión", exact=True).click()
    page.wait_for_url("**#ajustes/test/import-*")
    page.get_by_role("heading", name=CUSTOM["name"], exact=True).wait_for()
    page.screenshot(path=str(tmp_path / "fuente-incorporada.png"))
    assert not problems
    page.context.close()


def test_patient_service_survey_updates_owner_report(
    server: str, browser: Any, tmp_path: Path
) -> None:
    key = _staff_key(server)
    headers = {"X-API-Key": key}
    httpx.post(
        server + "/v1/crm/patients",
        headers=SERVICE,
        json={"id": "encuesta-demo", "display_name": "Paciente ficticia"},
    ).raise_for_status()
    access = httpx.post(server + "/v1/crm/patients/encuesta-demo/access", headers=SERVICE).json()
    patient_headers = {"X-API-Key": access["key"]}
    for purpose in ("marketing", "analytics", "memory"):
        httpx.put(
            server + f"/v1/me/consents/{purpose}", headers=patient_headers, json={"granted": False}
        ).raise_for_status()
    httpx.put(
        server + "/v1/me/consents/surveys", headers=patient_headers, json={"granted": True}
    ).raise_for_status()
    definition = httpx.post(
        server + "/v1/surveys/definitions",
        headers=headers,
        json={
            "spec": {
                "title": "Atención de prueba",
                "questions": [{"id": "atencion", "text": "¿Cómo fue la atención?"}],
                "critical_below": 40,
            }
        },
    ).json()
    httpx.put(
        server + "/v1/surveys/configuration",
        headers=headers,
        json={
            "revision": 0,
            "settings": {
                "enabled": True,
                "survey_id": definition["id"],
                "version": 1,
                "after_minutes": 0,
            },
        },
    ).raise_for_status()
    visit = httpx.post(
        server + "/v1/crm/appointments",
        headers=SERVICE,
        json={
            "patient_id": "encuesta-demo",
            "starts_at": (utcnow() + timedelta(days=1)).isoformat(),
        },
    ).json()
    httpx.post(
        server + f"/v1/crm/appointments/{visit['id']}/status",
        headers=SERVICE,
        json={"status": "completed"},
    ).raise_for_status()
    page = browser.new_page()
    errors = _watch(page)
    page.add_init_script('localStorage.setItem("mi.key", ' + json.dumps(access["key"]) + ")")
    page.goto(server + "/mi/#encuestas")
    page.get_by_role("link", name="Responder esta encuesta", exact=True).click()
    page.get_by_role("radio", name="1", exact=True).check()
    page.get_by_role("button", name="Enviar mi valoración del servicio", exact=True).click()
    page.get_by_text("Gracias. Tu valoración quedó registrada.", exact=True).wait_for()
    staff, problems = _signed_in(browser, server, key)
    staff.goto(server + "/app#marketing/encuestas")
    staff.get_by_text("Valoración de satisfacción: 0 de 100.", exact=True).wait_for()
    staff.screenshot(path=str(tmp_path / "encuesta-y-alerta.png"))
    staff.get_by_role("button", name="Marcar como revisada", exact=True).click()
    assert httpx.get(server + "/v1/surveys/report", headers=headers).json()["critical_alerts"] == []
    assert not errors and not problems
    page.close()
    staff.context.close()


def test_nonfiscal_trial_ui_has_no_sri_authorisation(
    server: str, browser: Any, settings: Settings, tmp_path: Path
) -> None:
    settings.billing_enabled = True
    httpx.put(
        server + "/v1/billing/bank",
        headers=SERVICE,
        json={"instructions": "Cuenta ficticia de prueba, no pagar"},
    ).raise_for_status()
    httpx.post(
        server + "/v1/crm/patients",
        headers=SERVICE,
        json={"id": "fiscal-demo", "display_name": "Paciente ficticia"},
    ).raise_for_status()
    visit = httpx.post(
        server + "/v1/crm/appointments",
        headers=SERVICE,
        json={
            "patient_id": "fiscal-demo",
            "starts_at": (utcnow() + timedelta(days=1)).isoformat(),
            "price": 50,
        },
    ).json()
    httpx.post(
        server + f"/v1/billing/appointments/{visit['id']}", headers=SERVICE
    ).raise_for_status()
    key = _staff_key(server)
    httpx.put(
        server + "/v1/tax-trials/issuers",
        headers={"X-API-Key": key},
        json={"version": 0, "profile": PROFILE},
    ).raise_for_status()
    page, problems = _signed_in(browser, server, key)
    page.goto(server + "/app#ajustes/fiscal-ensayo")
    page.get_by_label("Fundamento del importe para este ensayo", exact=True).fill(
        "Regla ficticia QA sin IVA real"
    )
    page.get_by_role("button", name="Generar ensayo no fiscal", exact=True).click()
    page.get_by_text(
        "ENSAYO LOCAL SIN VALIDEZ TRIBUTARIA. No se envió al SRI.", exact=True
    ).wait_for()
    page.screenshot(path=str(tmp_path / "ensayo-no-fiscal.png"))
    row = httpx.get(server + "/v1/tax-trials/documents", headers={"X-API-Key": key}).json()[0]
    assert row["outcome"]["sri_authorised"] is False and row["state"] == "simulated_accepted"
    assert not problems
    page.context.close()
