"""Actual clinical UI, fictional images; inference replaced by a labelled test double."""

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from orchestrator.config import Settings
from orchestrator.test_vision import PrivateVision, VisualExtraction
from tests.test_consultorio_e2e import (
    SERVICE,
    _signed_in,
    _staff_key,
)
from tests.test_consultorio_e2e import browser as browser
from tests.test_consultorio_e2e import server as server
from tests.test_consultorio_e2e import subprocess_loop as subprocess_loop
from tests.test_test_images import APPLICATION, PARAMETERS, image_bytes


def fill_context(modal: Any) -> None:
    modal.get_by_label("Versión o edición", exact=True).fill(PARAMETERS["version"])
    modal.get_by_label("Criterios e indicadores", exact=True).fill(PARAMETERS["criteria"])
    modal.get_by_label("Baremos o reglas de corrección", exact=True).fill(
        PARAMETERS["scoring_rules"]
    )
    modal.get_by_label("Fuente o manual", exact=True).fill(PARAMETERS["source"])
    modal.get_by_label("Población de referencia", exact=True).fill(
        PARAMETERS["reference_population"]
    )
    modal.get_by_label("Fecha de aplicación", exact=True).fill(APPLICATION["applied_on"])
    modal.get_by_label("Edad al aplicar (años)", exact=True).fill(str(APPLICATION["age_years"]))
    modal.get_by_label("Meses adicionales (0 a 11)", exact=True).fill(
        str(APPLICATION["age_months"])
    )
    modal.get_by_label("Contexto y observaciones", exact=True).fill(APPLICATION["context"])


@pytest.mark.parametrize("kind", ["questionnaire", "drawing"])
def test_upload_analyze_edit_review_and_download(
    server: str,
    browser: Any,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    tmp_path: Path,
) -> None:
    settings.clinical_vision_enabled = True
    settings.clinical_vision_model = "synthetic-vision-test"

    async def fictional_extraction(
        self: PrivateVision, image: bytes, image_kind: str
    ) -> VisualExtraction:
        assert image_kind == kind and image
        return VisualExtraction(
            visible_text="LECTURA FICTICIA",
            observations=["Elemento de prueba."],
            uncertainties=["Revisar con el original."],
        )

    monkeypatch.setattr(PrivateVision, "extract", fictional_extraction)
    key = _staff_key(server)
    httpx.post(
        f"{server}/v1/crm/patients",
        json={"id": "p-vision", "display_name": "Paciente ficticia"},
        headers=SERVICE,
    ).raise_for_status()
    page, problems = _signed_in(browser, server, key)
    page.set_viewport_size({"width": 1440, "height": 1100})
    if kind == "drawing":
        page.set_viewport_size({"width": 390, "height": 844})
    page.goto(f"{server}/app#paciente/p-vision/tests")
    page.get_by_role("button", name="Cargar test en imagen").click()
    modal = page.locator("#dialog")
    fill_context(modal)
    modal.get_by_label("Nombre del test", exact=True).fill("Imagen sintética para prueba")
    modal.get_by_label("Tipo de imagen").select_option(kind)
    modal.locator("input[accept='image/png,image/jpeg']").set_input_files(
        {"name": "synthetic.png", "mimeType": "image/png", "buffer": image_bytes()}
    )
    modal.locator(".test-image-preview:not([hidden])").wait_for()
    modal.evaluate("node => { node.scrollTop = 0; }")
    page.screenshot(path=str(tmp_path / f"{kind}-parametros.png"))
    modal.get_by_role("button", name="Cargar y generar borrador").click()
    modal.get_by_text("Borrador generado.", exact=False).wait_for()
    page.screenshot(path=str(tmp_path / f"{kind}-borrador.png"))
    assert "LECTURA FICTICIA" in modal.get_by_label("Borrador del informe").input_value()
    assert modal.get_by_role("button", name="Guardar informe revisado").is_disabled()
    modal.get_by_label("Borrador del informe").fill(
        "Informe corregido y revisado por la profesional. Datos ficticios."
    )
    modal.get_by_role("button", name="Guardar borrador", exact=True).click()
    modal.get_by_text("Borrador guardado.", exact=True).wait_for()
    modal.get_by_role("checkbox").check()
    modal.get_by_role("button", name="Guardar informe revisado").click()
    modal.get_by_text("Informe revisado y guardado.", exact=True).wait_for()
    assert modal.get_by_label("Borrador del informe").is_disabled()
    assert modal.get_by_label("Criterios e indicadores", exact=True).is_disabled()
    with page.expect_download() as downloaded:
        modal.get_by_role("button", name="Descargar informe (.txt)").click()
    assert downloaded.value.suggested_filename.endswith(".txt")
    exported = Path(downloaded.value.path()).read_text(encoding="utf-8")
    assert "INFORME REVISADO" in exported and "Fuente SHA-256" in exported
    assert "Informe corregido y revisado" in exported
    assert "Edición ficticia 2026" in exported and "30 años, 2 meses" in exported
    page.screenshot(path=str(tmp_path / f"{kind}-revisado.png"))
    assert modal.evaluate("node => node.scrollWidth <= node.clientWidth")
    modal.get_by_role("button", name="Cerrar", exact=True).click()
    page.get_by_role("button", name="Abrir informe").click()
    modal.get_by_text("Informe revisado", exact=True).wait_for()
    assert "Informe corregido" in modal.get_by_label("Borrador del informe").input_value()
    assert not problems, problems
    page.close()


def test_unconfigured_engine_uploads_without_fabricating_a_report(
    server: str, browser: Any
) -> None:
    key = _staff_key(server)
    httpx.post(
        f"{server}/v1/crm/patients",
        json={"id": "p-offline", "display_name": "Prueba sin motor"},
        headers=SERVICE,
    ).raise_for_status()
    page, problems = _signed_in(browser, server, key)
    page.goto(f"{server}/app#paciente/p-offline/tests")
    page.get_by_role("button", name="Cargar test en imagen").click()
    modal = page.locator("#dialog")
    modal.get_by_text("El motor de visión privado aún no está configurado.", exact=False).wait_for()
    modal.get_by_label("Nombre del test", exact=True).fill("Dibujo ficticio")
    modal.locator("input[accept='image/png,image/jpeg']").set_input_files(
        {"name": "synthetic.png", "mimeType": "image/png", "buffer": image_bytes()}
    )
    modal.get_by_role("button", name="Cargar imagen", exact=True).click()
    modal.get_by_text("Imagen guardada.", exact=True).wait_for()
    assert modal.get_by_label("Borrador del informe").input_value() == ""
    assert modal.get_by_role("button", name="Generar resumen visual").is_disabled()
    modal.get_by_role("button", name="Eliminar borrador e imagen").click()
    modal.get_by_role("button", name="Sí, eliminar este borrador y su imagen").click()
    page.get_by_text("Todavía no has cargado tests en imagen.", exact=True).wait_for()
    assert not problems, problems
    page.close()


def test_parameter_import_and_save_without_copying_application(server: str, browser: Any) -> None:
    key = _staff_key(server)
    httpx.post(
        f"{server}/v1/crm/patients",
        json={"id": "p-params", "display_name": "Parámetros ficticios"},
        headers=SERVICE,
    ).raise_for_status()
    page, problems = _signed_in(browser, server, key)
    page.goto(f"{server}/app#paciente/p-params/tests")
    page.get_by_role("button", name="Cargar test en imagen").click()
    modal = page.locator("#dialog")
    modal.get_by_label(
        "Importar parámetros desde un archivo JSON (opcional)", exact=True
    ).set_input_files(
        {
            "name": "parameters.json",
            "mimeType": "application/json",
            "buffer": json.dumps(PARAMETERS).encode(),
        }
    )
    modal.get_by_text("Parámetros importados.", exact=False).wait_for()
    assert modal.get_by_label("Fecha de aplicación", exact=True).input_value() == ""
    assert modal.get_by_label("Edad al aplicar (años)", exact=True).input_value() == ""
    modal.get_by_label("Fecha de aplicación", exact=True).fill("2026-11-03")
    modal.get_by_label("Nombre del test", exact=True).fill("Test con parámetros importados")
    modal.locator("input[accept='image/png,image/jpeg']").set_input_files(
        {"name": "synthetic.png", "mimeType": "image/png", "buffer": image_bytes()}
    )
    modal.get_by_role("button", name="Cargar imagen", exact=True).click()
    modal.get_by_text("Imagen guardada.", exact=True).wait_for()
    modal.locator("summary").click()
    assert (
        modal.get_by_label("Versión o edición", exact=True).input_value() == PARAMETERS["version"]
    )
    modal.get_by_label("Contexto y observaciones", exact=True).fill("Contexto nuevo de prueba")
    modal.get_by_role("button", name="Guardar parámetros", exact=True).click()
    modal.get_by_text("Borrador guardado.", exact=True).wait_for()
    modal.get_by_role("button", name="Cerrar", exact=True).click()
    page.get_by_role("button", name="Abrir informe", exact=True).click()
    modal.locator("summary").click()
    assert (
        modal.get_by_label("Contexto y observaciones", exact=True).input_value()
        == "Contexto nuevo de prueba"
    )
    with page.expect_download() as downloaded:
        modal.get_by_role("button", name="Descargar parámetros para reutilizar").click()
    exported = json.loads(Path(downloaded.value.path()).read_text(encoding="utf-8"))
    assert exported == PARAMETERS and "application" not in exported
    assert not problems, problems
    page.close()
