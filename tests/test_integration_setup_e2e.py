"""Browser review of preparation forms with synthetic data, without vendor traffic."""

import sys
from typing import Any

import httpx
import pytest

from tests.test_consultorio_e2e import _signed_in, _staff_key
from tests.test_consultorio_e2e import server as server
from tests.test_consultorio_e2e import subprocess_loop as subprocess_loop

sync_api = pytest.importorskip("playwright.sync_api")


@pytest.fixture
def chrome(subprocess_loop: None) -> Any:
    with sync_api.sync_playwright() as pw:
        # The owner's Windows review uses Chrome; Linux CI installs Chromium.
        browser = (
            pw.chromium.launch(channel="chrome", headless=True)
            if sys.platform == "win32"
            else pw.chromium.launch(headless=True)
        )
        yield browser
        browser.close()


def test_save_reload_and_clear_references_without_claiming_connection(
    server: str, chrome: Any
) -> None:
    key = _staff_key(server)
    page, problems = _signed_in(chrome, server, key)
    external: list[str] = []
    page.route(
        "https://**/*",
        lambda route: (external.append(route.request.url.split("?")[0]), route.abort()),
    )
    page.goto(server + "/app#ajustes/integraciones")
    meta = page.get_by_role("region", name="Facebook, Instagram y WhatsApp", exact=True)
    meta.get_by_label("RUC de mi empresa (13 dígitos)", exact=True).fill("0000000000001")
    meta.get_by_label("ID del portafolio empresarial de Meta", exact=True).fill("123456789000")
    meta.get_by_role("button", name="Guardar datos de mi cuenta", exact=True).click()
    sync_api.expect(meta.get_by_role("status")).to_contain_text("Datos guardados")
    page.reload()
    sync_api.expect(meta.get_by_label("RUC de mi empresa (13 dígitos)")).to_have_value(
        "0000000000001"
    )
    vendor = page.get_by_role("region", name="TuFacturero", exact=True)
    vendor.get_by_label("RUC de mi empresa (13 dígitos)", exact=True).fill("0000000000001")
    vendor.get_by_role("button", name="Guardar datos de mi cuenta", exact=True).click()
    sync_api.expect(vendor.get_by_role("status")).to_contain_text("autorización del proveedor")
    mental = page.get_by_role("region", name="MentalTestLab", exact=True)
    mental.get_by_label("Referencia de tu cuenta en MentalTestLab", exact=True).fill("qa-demo")
    mental.get_by_role("button", name="Guardar datos de mi cuenta", exact=True).click()
    sync_api.expect(mental.get_by_role("status")).to_contain_text("Datos guardados")
    mental.get_by_role("button", name="Eliminar datos guardados", exact=True).click()
    mental.get_by_role("button", name="Sí, eliminar datos", exact=True).click()
    sync_api.expect(mental.get_by_label("Referencia de tu cuenta en MentalTestLab")).to_have_value(
        ""
    )
    page.reload()
    entries = httpx.get(server + "/v1/integration-setup", headers={"X-API-Key": key}).json()
    assert [entry["configured"] for entry in entries] == [True, True, False]
    assert all(entry["external_connection"] == "not_checked" for entry in entries)
    page.set_viewport_size({"width": 390, "height": 844})
    sync_api.expect(
        page.get_by_role("heading", name="Integraciones del consultorio")
    ).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    assert not external and not problems
    page.close()
