"""Browser membership checkboxes and two separate campaign audiences; no sends."""

from pathlib import Path
from typing import Any

import httpx

from tests.test_consultorio_e2e import SERVICE, _signed_in
from tests.test_consultorio_e2e import browser as browser
from tests.test_consultorio_e2e import server as server
from tests.test_consultorio_e2e import subprocess_loop as subprocess_loop


def test_group_checkboxes_persist_and_campaigns_target_separate_groups(
    server: str,
    browser: Any,
    tmp_path: Path,
) -> None:
    response = httpx.post(
        f"{server}/v1/admin/staff",
        headers=SERVICE,
        json={
            "name": "marketing.demo",
            "roles": ["marketing"],
        },
    )
    response.raise_for_status()
    key = response.json()["key"]
    for pid, name in [("grupo-ana", "Ana Ficticia"), ("grupo-bea", "Bea Ficticia")]:
        httpx.post(
            f"{server}/v1/crm/patients",
            headers=SERVICE,
            json={
                "id": pid,
                "display_name": name,
                "telegram_chat_id": "123456",
            },
        ).raise_for_status()
        httpx.put(
            f"{server}/v1/subjects/{pid}/consents/marketing",
            headers=SERVICE,
            json={"granted": True, "source": "form"},
        ).raise_for_status()
    page, problems = _signed_in(browser, server, key)
    page.set_viewport_size({"width": 1440, "height": 1050})
    page.goto(f"{server}/app#marketing/grupos")
    for name, person in [("Grupo A", "Ana Ficticia"), ("Grupo B", "Bea Ficticia")]:
        page.get_by_role("main").get_by_label("Nombre del grupo", exact=True).fill(name)
        page.get_by_role("button", name="Crear grupo", exact=True).click()
        modal = page.locator("#dialog")
        modal.get_by_role("checkbox", name=person, exact=True).check()
        modal.get_by_role("button", name="Guardar grupo y personas", exact=True).click()
        page.locator("#dialog").wait_for(state="hidden")
        page.get_by_role("heading", name=name, exact=True).wait_for()
    first = page.locator(".card").filter(
        has=page.get_by_role("heading", name="Grupo A", exact=True)
    )
    first.get_by_role("button", name="Elegir personas").click()
    modal = page.locator("#dialog")
    assert modal.get_by_role("checkbox", name="Ana Ficticia", exact=True).is_checked()
    assert not modal.get_by_role("checkbox", name="Bea Ficticia", exact=True).is_checked()
    assert modal.get_by_role("checkbox", name="Bea Ficticia", exact=True).is_disabled()
    page.screenshot(path=str(tmp_path / "grupos-casillas.png"))
    modal.get_by_role("button", name="Cerrar", exact=True).click()
    for name in ["Grupo A", "Grupo B"]:
        page.goto(f"{server}/app#marketing/campanas")
        form = page.locator("form").filter(has=page.get_by_role("button", name="Crear campaña"))
        form.get_by_label("Nombre", exact=True).fill(f"Publicidad {name}")
        form.get_by_label("A quiénes", exact=True).select_option("manual_groups")
        form.get_by_role("checkbox", name=f"{name} (1)", exact=True).check()
        form.get_by_role("button", name="Crear campaña", exact=True).click()
        page.get_by_text(f"Publicidad {name}", exact=True).wait_for()
    page.screenshot(path=str(tmp_path / "publicidad-por-grupos.png"))
    rows = httpx.get(f"{server}/v1/campaigns", headers=SERVICE).json()
    assert {c["name"]: [g["name"] for g in c["audience_groups"]] for c in rows} == {
        "Publicidad Grupo A": ["Grupo A"],
        "Publicidad Grupo B": ["Grupo B"],
    }
    assert all(c["population"]["segment"] == 1 and c["mode"] == "simulation" for c in rows)
    assert not problems, problems
    page.close()


def test_catalog_marketing_agent_fills_campaign_and_publication_drafts(
    server: str,
    browser: Any,
) -> None:
    response = httpx.post(
        f"{server}/v1/admin/staff",
        headers=SERVICE,
        json={
            "name": "design.demo",
            "roles": ["marketing"],
        },
    )
    response.raise_for_status()
    key = response.json()["key"]
    httpx.post(
        f"{server}/v1/social/accounts",
        headers=SERVICE,
        json={
            "network": "instagram",
            "external_id": "synthetic-account",
            "handle": "@demo",
            "secret_ref": "SYNTHETIC_DESIGN_ACCOUNT",
        },
    ).raise_for_status()
    page, problems = _signed_in(browser, server, key)
    page.goto(f"{server}/app#marketing/campanas")
    page.get_by_role("button", name="Agente de marketing", exact=True).click()
    modal = page.locator("#dialog")
    modal.get_by_label("Qué quieres promocionar", exact=True).fill("Novedades del consultorio")
    modal.get_by_role("button", name="Diseñar campaña y publicidad", exact=True).click()
    modal.get_by_text("Ejemplo simulado: revisa y edita antes de usar.", exact=True).wait_for()
    modal.get_by_role("button", name="Usar en campaña", exact=True).click()
    form = (
        page.get_by_role("main")
        .locator("form")
        .filter(has=page.get_by_role("button", name="Crear campaña", exact=True))
    )
    assert form.get_by_label("Nombre", exact=True).input_value().startswith("Novedades")
    assert "STOP" in form.locator("textarea").input_value()
    assert httpx.get(f"{server}/v1/campaigns", headers=SERVICE).json() == []
    page.get_by_role("button", name="Agente de marketing", exact=True).click()
    modal.get_by_label("Qué quieres promocionar", exact=True).fill("Novedades del consultorio")
    modal.get_by_role("button", name="Diseñar campaña y publicidad", exact=True).click()
    modal.get_by_role("button", name="Usar en publicación", exact=True).click()
    page.wait_for_url("**#marketing/publicaciones")
    assert httpx.get(f"{server}/v1/social/accounts", headers={"X-API-Key": key}).json()
    assert not problems, problems
    try:
        page.get_by_role("heading", name="Por publicar", exact=True).wait_for(timeout=5000)
    except Exception:
        raise AssertionError(page.get_by_role("main").inner_text()) from None
    assert page.get_by_role("button", name="Crear la imagen", exact=True).count() == 1, (
        page.get_by_role("main").inner_text()
    )
    publication = (
        page.get_by_role("main")
        .locator("form")
        .filter(has=page.get_by_role("button", name="Crear la imagen", exact=True))
    )
    assert (
        publication.get_by_label("Título de la imagen", exact=True).input_value()
        == "Un espacio para ti"
    )
    assert publication.get_by_label("Texto de la publicación", exact=True).input_value()
    assert httpx.get(f"{server}/v1/social/publications", headers=SERVICE).json() == []
    assert not problems, problems
    page.close()
