"""All three connect buttons, with provider pages and APIs simulated. No real Meta."""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from pydantic import SecretStr

from orchestrator.config import Settings
from orchestrator.db import utcnow
from orchestrator.meta_login import FB_SCOPES, WA_SCOPES, MetaLogin
from tests.test_consultorio_e2e import _signed_in, _staff_key
from tests.test_consultorio_e2e import browser as browser
from tests.test_consultorio_e2e import server as server
from tests.test_consultorio_e2e import subprocess_loop as subprocess_loop


@pytest.fixture
def social_server(server: str, settings: Settings, monkeypatch: pytest.MonkeyPatch) -> str:
    settings.social_login_enabled = True
    settings.social_login_origin = server  # local-only test; production requires HTTPS
    settings.meta_app_id, settings.meta_login_config_id, settings.meta_whatsapp_config_id = (
        "100",
        "200",
        "300",
    )
    settings.instagram_app_id = "400"
    settings.instagram_app_secret = SecretStr("synthetic-instagram-secret")

    async def fake(self: MetaLogin, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        path = urlsplit(url).path
        if path.endswith("/debug_token"):
            return {
                "data": {
                    "is_valid": True,
                    "app_id": "100",
                    "scopes": sorted(FB_SCOPES | WA_SCOPES),
                    "expires_at": int((utcnow() + timedelta(days=60)).timestamp()),
                    "granular_scopes": [
                        {"scope": "whatsapp_business_management", "target_ids": ["600"]}
                    ],
                }
            }
        if urlsplit(url).hostname == "api.instagram.com":
            return {"access_token": "fake-short", "user_id": "700"}
        if path == "/access_token":
            return {"access_token": "fake-long", "expires_in": 5_184_000}
        if path.endswith("/me"):
            return {"user_id": "700", "username": "@perfil.ficticio"}
        if path.endswith("/oauth/access_token"):
            return {"access_token": "fake-user"}
        if path.endswith("/me/accounts"):
            return {
                "data": [
                    {
                        "id": "500",
                        "name": "Página ficticia <img src=x>",
                        "access_token": "fake-page",
                        "tasks": ["CREATE_CONTENT", "MESSAGING"],
                    }
                ]
            }
        if path.endswith("/600/phone_numbers"):
            return {
                "data": [
                    {
                        "id": "800",
                        "verified_name": "Número ficticio",
                        "display_phone_number": "+15550001111",
                    }
                ]
            }
        if path.endswith("/subscribed_apps"):
            return {"success": True}
        raise AssertionError(path)

    monkeypatch.setattr(MetaLogin, "_request", fake)
    return server


@pytest.mark.parametrize(
    "network,label",
    [("facebook", "Facebook"), ("instagram", "Instagram"), ("whatsapp", "WhatsApp")],
)
def test_connect_by_login_without_id_fields(
    social_server: str,
    browser: Any,
    network: str,
    label: str,
    tmp_path: Any,
) -> None:
    page, problems = _signed_in(browser, social_server, _staff_key(social_server))

    def redirect(route: Any) -> None:
        state = parse_qs(urlsplit(route.request.url).query)["state"][0]
        url = (
            social_server
            + f"/v1/social/login/{network}/callback?"
            + urlencode({"code": "fake-code", "state": state})
        )
        route.fulfill(status=302, headers={"Location": url}, body="")

    if network == "whatsapp":
        page.route(
            "https://connect.facebook.net/**",
            lambda route: route.fulfill(
                content_type="application/javascript",
                body=(
                    "window.FB={init(){},login(cb,options){"
                    "if(options.response_type!=='code')throw Error('code required');"
                    "cb({authResponse:{code:'fake-code'}})}};window.fbAsyncInit();"
                ),
            ),
        )
    else:
        host = "www.instagram.com" if network == "instagram" else "www.facebook.com"
        page.route(f"https://{host}/**", redirect)
    page.goto(social_server + "/app#ajustes/redes")
    page.get_by_role("button", name=f"Conectar {label}", exact=True).wait_for()
    assert page.get_by_label("Nombre de la cuenta", exact=True).count() == 0
    assert not page.locator("input:visible").count()
    page.get_by_role("button", name=f"Conectar {label}", exact=True).click()
    if network == "whatsapp":
        page.get_by_role("button", name="Continuar con Meta", exact=True).click()
    page.get_by_role("heading", name="Elige la cuenta que quieres conectar", exact=True).wait_for()
    choice = page.get_by_role("region", name="Elige la cuenta que quieres conectar", exact=True)
    choice.get_by_role("button").click()
    account = page.locator(".social-account")
    account.get_by_text("Conectada", exact=True).wait_for()
    assert account.locator("img").count() == 0
    assert "fake-page" not in page.inner_text("body") and "fake-long" not in page.inner_text("body")
    page.reload()
    page.locator(".social-account").get_by_text("Conectada", exact=True).wait_for()
    page.set_viewport_size({"width": 390, "height": 844})
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.screenshot(path=str(tmp_path / (network + "-conexion.png")), full_page=True)
    page.locator(".social-account").get_by_role("button").click()
    page.get_by_role("button", name="Sí, desconectar", exact=True).click()
    page.locator(".social-account").wait_for(state="detached")
    assert not problems, problems
