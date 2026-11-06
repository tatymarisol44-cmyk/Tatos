"""Private UI changes templates and advances a fictional clock; no WhatsApp links open."""

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from orchestrator.config import Settings
from tests.test_consultorio_e2e import SERVICE, _signed_in, _staff_key
from tests.test_consultorio_e2e import browser as browser
from tests.test_consultorio_e2e import server as server
from tests.test_consultorio_e2e import subprocess_loop as subprocess_loop


def test_private_notice_texts_clock_and_history(
    server: str, browser: Any, settings: Settings, tmp_path: Path
) -> None:
    settings.billing_enabled = True
    httpx.put(
        server + "/v1/billing/bank",
        headers=SERVICE,
        json={"instructions": "Banco ficticio del consultorio"},
    ).raise_for_status()
    httpx.post(
        server + "/v1/crm/patients",
        headers=SERVICE,
        json={"id": "notice-demo", "display_name": "Paciente Ficticia", "phone": "+12025550123"},
    ).raise_for_status()
    httpx.put(
        server + "/v1/subjects/notice-demo/consents/whatsapp_billing",
        headers=SERVICE,
        json={"granted": True, "source": "Permiso de prueba ficticio"},
    ).raise_for_status()
    due = datetime.now(ZoneInfo("America/Guayaquil")).replace(
        hour=10, minute=0, second=0, microsecond=0
    )
    visit = httpx.post(
        server + "/v1/crm/appointments",
        headers=SERVICE,
        json={
            "patient_id": "notice-demo",
            "starts_at": (due + timedelta(days=1)).isoformat(),
            "price": 49.99,
        },
    ).json()
    invoice = httpx.post(server + f"/v1/billing/appointments/{visit['id']}", headers=SERVICE).json()
    base = server + f"/v1/billing/orders/{invoice['invoice_id']}"
    httpx.put(
        base + "/deadline", headers=SERVICE, json={"revision": 1, "due_at": due.isoformat()}
    ).raise_for_status()
    page, problems = _signed_in(browser, server, _staff_key(server))
    page.goto(server + "/app#ajustes/pagos")
    page.get_by_role("button", name="Ver pago", exact=True).click()
    page.get_by_role("button", name="Saldo, vencimiento y abonos", exact=True).click()
    page.get_by_role("button", name="Ensayar avisos sin enviar", exact=True).click()
    modal = page.locator("#dialog")
    modal.get_by_label("Primer seguimiento", exact=True).fill(
        "Revisa tu saldo de {balance} en la app."
    )
    modal.get_by_role("button", name="Guardar textos del ensayo", exact=True).click()
    modal.get_by_text("Textos guardados para la simulación.", exact=False).wait_for()
    modal.get_by_label("Fecha y hora ficticia (America/Guayaquil)", exact=True).fill(
        due.replace(tzinfo=None).isoformat(timespec="minutes")
    )
    modal.get_by_role("button", name="Simular este momento", exact=True).click()
    modal.get_by_text("El día del vencimiento · SIMULADO", exact=True).wait_for()
    modal.get_by_role("button", name="Simular este momento", exact=True).click()
    modal.get_by_text("Este aviso ya se ensayó.", exact=True).wait_for()
    modal.get_by_role("button", name="Avanzar un día ficticio", exact=True).click()
    modal.get_by_role("button", name="Simular este momento", exact=True).click()
    modal.get_by_text("Primer seguimiento · SIMULADO", exact=True).wait_for()
    modal.get_by_text("Revisa tu saldo de USD 49.99 en la app.", exact=True).wait_for()
    rows = httpx.get(base + "/notice-trials", headers=SERVICE).json()
    assert len(rows) == 2 and all(r["provider_message_id"] is None for r in rows)
    page.screenshot(path=str(tmp_path / "avisos-simulados.png"))
    assert not problems, problems
    page.close()
