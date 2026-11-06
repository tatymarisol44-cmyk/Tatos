"""Real browser scoring flow; fictional answers and a mocked vision provider."""

from pathlib import Path
from typing import Any

import httpx
import pytest

from orchestrator.config import Settings
from orchestrator.test_scoring import PixelAnswer
from orchestrator.test_vision import PrivateVision, VisualExtraction
from tests.test_consultorio_e2e import SERVICE, _signed_in, _staff_key
from tests.test_consultorio_e2e import browser as browser
from tests.test_consultorio_e2e import server as server
from tests.test_consultorio_e2e import subprocess_loop as subprocess_loop
from tests.test_image_scoring import SPEC
from tests.test_test_images import image_bytes
from tests.test_test_images_e2e import fill_context


def test_configure_bands_read_regions_confirm_and_score(
    server: str,
    browser: Any,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings.clinical_vision_enabled = True
    settings.clinical_vision_model = "synthetic-vision"

    async def fictional(
        self: PrivateVision, content: bytes, kind: str, items: Any = None
    ) -> VisualExtraction:
        assert kind == "questionnaire" and len(items) == 3
        return VisualExtraction(
            visible_text="Prueba ficticia",
            observations=[],
            uncertainties=[],
            answers=[
                PixelAnswer(
                    item_id=key,
                    value=value,
                    quality="clear",
                    evidence="Marca ficticia de prueba",
                    region=(0.1, 0.1, 0.7, 0.3),
                )
                for key, value in {"i1": 2, "i2": 1, "i3": 0}.items()
            ],
        )

    monkeypatch.setattr(PrivateVision, "extract", fictional)
    key = _staff_key(server)
    headers = {"X-API-Key": key}
    definition = httpx.post(
        f"{server}/v1/clinical/instruments", json={"spec": SPEC}, headers=headers
    ).json()
    httpx.post(
        f"{server}/v1/crm/patients",
        json={"id": "p-bands", "display_name": "Paciente ficticia"},
        headers=SERVICE,
    ).raise_for_status()
    page, problems = _signed_in(browser, server, key)
    page.set_viewport_size({"width": 1440, "height": 1100})
    page.goto(f"{server}/app#ajustes/test/{definition['instrument_id']}")
    form = page.locator("form").filter(
        has=page.get_by_role("button", name="Guardar puntuación y bandas")
    )
    form.get_by_label("Nombre de la banda", exact=True).nth(1).fill("Grupo ficticio B actualizado")
    form.get_by_role(
        "checkbox", name="He comprobado valores, rangos y correspondencia con el baremo del test."
    ).check()
    form.get_by_role("button", name="Guardar puntuación y bandas").click()
    page.get_by_text("Puntuación y bandas guardadas · versión 2.", exact=True).wait_for()
    page.screenshot(path=str(tmp_path / "bandas-configuradas.png"))
    page.goto(f"{server}/app#paciente/p-bands/tests")
    page.get_by_role("button", name="Cargar test en imagen").click()
    modal = page.locator("#dialog")
    fill_context(modal)
    modal.get_by_label("Nombre del test", exact=True).fill("Cuestionario ficticio desde imagen")
    modal.get_by_label("Test y versión para calcular bandas", exact=True).select_option(
        f"{definition['instrument_id']}@2"
    )
    modal.locator("input[accept='image/png,image/jpeg']").set_input_files(
        {"name": "synthetic.png", "mimeType": "image/png", "buffer": image_bytes()}
    )
    modal.get_by_role("button", name="Cargar y generar borrador").click()
    modal.get_by_text("Borrador generado.", exact=False).wait_for()
    assert modal.locator("canvas").count() == 3
    calculate = modal.get_by_role("button", name="Calcular puntuación y banda", exact=True)
    assert calculate.is_disabled()
    modal.get_by_role(
        "checkbox",
        name="Comparé todas las respuestas con la imagen y comprobé el baremo seleccionado.",
    ).check()
    calculate.click()
    modal.get_by_text("Puntuación: 4 · Banda: Grupo ficticio B actualizado", exact=True).wait_for()
    page.screenshot(path=str(tmp_path / "imagen-respuestas-banda.png"))
    modal.get_by_role(
        "checkbox", name="He comparado el informe con el original y revisado su contenido."
    ).check()
    modal.get_by_role("button", name="Guardar informe revisado", exact=True).click()
    modal.get_by_text("Informe revisado y guardado.", exact=True).wait_for()
    with page.expect_download() as downloaded:
        modal.get_by_role("button", name="Descargar informe (.txt)").click()
    report = Path(downloaded.value.path()).read_text(encoding="utf-8")
    assert "Grupo ficticio B actualizado" in report and "configuración 2" in report
    assert not problems, problems
    page.close()
