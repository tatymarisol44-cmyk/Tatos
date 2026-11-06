"""Real UI and files, synthetic LLM. Never invokes publication or messaging."""

from pathlib import Path
from typing import Any

import httpx
import pytest

from orchestrator.catalog import Catalog, load_catalog
from tests.test_consultorio_e2e import SERVICE, _signed_in, _staff_key
from tests.test_consultorio_e2e import browser as browser
from tests.test_consultorio_e2e import server as server
from tests.test_consultorio_e2e import subprocess_loop as subprocess_loop
from tests.test_marketing_workflow import real_specialists


@pytest.fixture
def catalog() -> Catalog:
    return real_specialists(load_catalog(Path(__file__).parent / "fixtures" / "agents"))


def test_brand_plan_edit_render_preview_and_pending_publications(
    server: str,
    browser: Any,
    tmp_path: Path,
) -> None:
    key = _staff_key(server)
    httpx.post(
        server + "/v1/social/accounts",
        headers=SERVICE,
        json={
            "network": "instagram",
            "external_id": "synthetic-marketing",
            "handle": "@ficticio",
            "secret_ref": "SYNTHETIC_WORKFLOW_NO_CREDENTIAL",
        },
    ).raise_for_status()
    page, problems = _signed_in(browser, server, key)
    page.goto(server + "/app#marketing/identidad")
    page.get_by_label("Nombre del consultorio para publicidad", exact=True).fill(
        "Consultorio de Prueba"
    )
    page.get_by_label("Servicios públicos (uno por línea)", exact=True).fill(
        "Orientación\nConsulta virtual"
    )
    page.get_by_role("button", name="Guardar identidad", exact=True).click()
    page.get_by_text("Identidad guardada.", exact=False).wait_for()
    page.get_by_role("link", name="Preparar un plan con esta identidad", exact=True).click()
    page.get_by_label("Objetivo público de la campaña", exact=True).fill(
        "Presentar los servicios públicos"
    )
    page.get_by_role("button", name="Preparar plan y contenido", exact=True).click()
    page.get_by_role("button", name="Revisar plan y diseños", exact=True).click()
    modal = page.locator("#dialog")
    modal.get_by_label("Título de la publicidad", exact=True).fill("Un espacio de orientación")
    modal.get_by_role("button", name="Guardar y generar diseños", exact=True).click()
    modal.wait_for(state="hidden")
    page.get_by_role("button", name="Ver diseño", exact=True).first.wait_for()
    page.get_by_role("button", name="Ver diseño", exact=True).first.click()
    image = page.get_by_alt_text("Diseño generado con la identidad del consultorio", exact=True)
    image.wait_for()
    page.wait_for_function("document.querySelector('#dialog img')?.naturalWidth === 1080")
    page.screenshot(path=str(tmp_path / "marketing-diseno-generado.png"))
    page.locator("#dialog").get_by_role("button", name="Cerrar", exact=True).click()
    page.get_by_role("link", name="Aprobar o programar publicaciones", exact=True).click()
    page.get_by_role("heading", name="Por publicar", exact=True).wait_for()
    files = httpx.get(server + "/v1/social/publications", headers=SERVICE).json()
    assert len(files) == 3 and all(f["status"] == "pending_approval" for f in files)
    assert all(f["approved_at"] is None and f["external_id"] is None for f in files)
    assert not problems, problems
    page.close()
