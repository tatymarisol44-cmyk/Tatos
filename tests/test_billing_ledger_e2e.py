"""Record a fictional bank credit, then verify the real patient's remaining balance."""

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


def test_admin_confirms_an_abono_and_patient_sees_only_the_remaining_balance(
    server: str,
    browser: Any,
    settings: Settings,
    tmp_path: Path,
) -> None:
    settings.billing_enabled = True
    httpx.put(
        server + "/v1/billing/bank",
        headers=SERVICE,
        json={"instructions": "Cuenta bancaria ficticia del consultorio"},
    ).raise_for_status()
    httpx.post(
        server + "/v1/crm/patients",
        headers=SERVICE,
        json={"id": "abono-demo", "display_name": "Paciente de prueba"},
    ).raise_for_status()
    visit = httpx.post(
        server + "/v1/crm/appointments",
        headers=SERVICE,
        json={
            "patient_id": "abono-demo",
            "starts_at": (utcnow() + timedelta(days=1)).isoformat(),
            "price": 49.99,
            "duration_min": 50,
        },
    ).json()
    invoice = httpx.post(server + f"/v1/billing/appointments/{visit['id']}", headers=SERVICE)
    invoice.raise_for_status()
    key = _staff_key(server)
    page, problems = _signed_in(browser, server, key)
    page.goto(server + "/app#ajustes/pagos")
    page.get_by_role("button", name="Ver pago", exact=True).click()
    page.get_by_role("button", name="Saldo, vencimiento y abonos", exact=True).click()
    modal = page.locator("#dialog")
    modal.get_by_label("Importe recibido en USD", exact=True).fill("10,00")
    modal.get_by_label("Identificador único del ingreso bancario", exact=True).fill(
        "banco-ficticio-abono"
    )
    modal.get_by_label("Nota de verificación del ingreso", exact=True).fill(
        "Ingreso ficticio verificado"
    )
    modal.get_by_role(
        "checkbox", name="Verifiqué que este ingreso llegó a la cuenta receptora", exact=True
    ).check()
    modal.get_by_role("button", name="Registrar abono confirmado", exact=True).click()
    modal.get_by_text("banco-ficticio-abono", exact=False).wait_for()
    page.screenshot(path=str(tmp_path / "abono-y-saldo.png"))
    data = httpx.get(
        server + f"/v1/billing/orders/{invoice.json()['invoice_id']}/statement", headers=SERVICE
    ).json()
    assert data["paid_cents"] == 1000 and data["balance_cents"] == 3999
    access = httpx.post(server + "/v1/crm/patients/abono-demo/access", headers=SERVICE).json()
    for purpose in ("marketing", "analytics", "memory"):
        httpx.put(
            server + f"/v1/me/consents/{purpose}",
            headers={"X-API-Key": access["key"]},
            json={"granted": False},
        ).raise_for_status()
    patient = browser.new_page()
    patient_errors = _watch(patient)
    patient.add_init_script('localStorage.setItem("mi.key", ' + json.dumps(access["key"]) + ")")
    patient.goto(server + "/mi/#pagos")
    patient.get_by_text("Total:", exact=False).wait_for()
    text = patient.locator("#view").inner_text()
    assert "39,99" in text or "39.99" in text
    assert "banco-ficticio-abono" not in text
    assert patient.get_by_role("button", name="Pagar con tarjeta", exact=True).count() == 0
    assert not problems + patient_errors
    page.close()
    patient.close()
