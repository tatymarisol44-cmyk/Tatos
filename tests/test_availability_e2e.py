"""Doctor date overrides and service selection in the real patient UI; synthetic only."""

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from tests.test_consultorio_e2e import SERVICE, _signed_in, _staff_key, _watch
from tests.test_consultorio_e2e import browser as browser
from tests.test_consultorio_e2e import server as server
from tests.test_consultorio_e2e import subprocess_loop as subprocess_loop


def test_doctor_settings_and_patient_services_use_the_same_schedule(
    server: str,
    browser: Any,
    tmp_path: Path,
) -> None:
    key = _staff_key(server)
    httpx.post(
        server + "/v1/admin/professionals",
        headers=SERVICE,
        json={
            "professional_id": "demo",
            "display_name": "Profesional Ficticia",
            "staff_id": "dra.demo",
            "pack_id": "ec-psychologist",
        },
    ).raise_for_status()
    httpx.put(
        server + "/v1/agenda/professionals/demo/hours",
        headers=SERVICE,
        json={
            "hours": [
                {"day": d, "hours": "09:00-13:00", "slot_minutes": 50}
                for d in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
            ],
        },
    ).raise_for_status()
    page, problems = _signed_in(browser, server, key)
    page.goto(server + "/app#ajustes")
    page.get_by_role("button", name="Agregar servicio de reserva", exact=True).click()
    page.get_by_label("Nombre del servicio", exact=True).fill("Consulta virtual ficticia")
    page.get_by_label("Minutos del servicio", exact=True).fill("30")
    page.get_by_label("Intervalo del servicio", exact=True).fill("15")
    page.get_by_label("Modalidad del servicio", exact=True).select_option("virtual")
    page.get_by_role("button", name="Guardar servicios de reserva", exact=True).click()
    page.get_by_text("Servicios guardados.", exact=False).wait_for()
    day = (datetime.now(ZoneInfo("America/Guayaquil")).date() + timedelta(days=1)).isoformat()
    page.get_by_label("Primera fecha", exact=True).fill(day)
    page.get_by_label("Última fecha", exact=True).fill(day)
    page.get_by_role("button", name="Guardar excepción", exact=True).click()
    page.get_by_text("Guardado. Las citas existentes se conservan", exact=False).wait_for()
    httpx.post(
        server + "/v1/crm/patients",
        headers=SERVICE,
        json={
            "id": "agenda-demo",
            "display_name": "Paciente Sintética",
        },
    ).raise_for_status()
    access = httpx.post(server + "/v1/crm/patients/agenda-demo/access", headers=SERVICE).json()
    for purpose in ("marketing", "analytics", "memory"):
        httpx.put(
            server + f"/v1/me/consents/{purpose}",
            headers={"X-API-Key": access["key"]},
            json={"granted": False},
        ).raise_for_status()
    patient = browser.new_page()
    patient_problems = _watch(patient)
    patient.add_init_script('localStorage.setItem("mi.key", ' + json.dumps(access["key"]) + ")")
    patient.goto(server + "/mi/#citas")
    patient.get_by_label("Servicio de reserva", exact=True).select_option(
        label="Consulta virtual ficticia · 30 min · Virtual"
    )
    patient.locator(".time-btn").first.wait_for()
    patient.locator(".time-btn").first.click()
    patient.get_by_role("button", name="Reservar el", exact=False).click()
    patient.get_by_text("Tu cita quedó reservada", exact=False).wait_for()
    visits = httpx.get(server + "/v1/crm/appointments", headers=SERVICE).json()
    assert len(visits) == 1
    assert visits[0]["duration_min"] == 30 and visits[0]["buffer_minutes"] == 15
    assert visits[0]["consultation_mode"] == "virtual"
    assert not visits[0]["starts_at"].startswith(day)
    page.screenshot(path=str(tmp_path / "agenda-excepciones.png"))
    assert not problems + patient_problems
    page.close()
    patient.close()
