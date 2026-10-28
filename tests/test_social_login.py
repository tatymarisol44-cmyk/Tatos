"""Meta contracts simulated at the HTTP boundary; no real accounts or messages."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from collections.abc import Iterator
from datetime import timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import insert, select, update

from orchestrator.api.app import create_app
from orchestrator.catalog import Catalog
from orchestrator.config import Settings
from orchestrator.db import utcnow
from orchestrator.llm import FakeLLM
from orchestrator.meta_login import FB_SCOPES, WA_SCOPES
from orchestrator.service import Orchestrator
from orchestrator.social import channel_accounts
from orchestrator.social_credentials import ReconnectRequired, credentials
from orchestrator.social_login import flows
from orchestrator.telemetry import _scrub

ACME = {"X-API-Key": "test-key"}
GLOBEX = {"X-API-Key": "other-key"}


@pytest.fixture
def login(
    settings: Settings, catalog: Catalog
) -> Iterator[tuple[TestClient, Orchestrator, dict[str, Any]]]:
    settings.social_login_enabled = True
    settings.social_login_origin = "https://testserver"
    settings.meta_app_id = "100"
    settings.meta_app_secret = SecretStr("synthetic-app-secret")
    settings.meta_login_config_id = "200"
    settings.meta_whatsapp_config_id = "300"
    settings.instagram_app_id = "400"
    settings.instagram_app_secret = SecretStr("synthetic-instagram-secret")
    options: dict[str, Any] = {"calls": []}

    def provider(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        options["calls"].append(path)
        if path.endswith("/debug_token"):
            return httpx.Response(
                200,
                json={
                    "data": {
                        "is_valid": True,
                        "app_id": options.get("app_id", "100"),
                        "scopes": options.get("scopes", sorted(FB_SCOPES | WA_SCOPES)),
                        "expires_at": int((utcnow() + timedelta(days=60)).timestamp()),
                        "granular_scopes": [
                            {"scope": "whatsapp_business_management", "target_ids": ["600"]}
                        ],
                    }
                },
            )
        if request.url.host == "api.instagram.com":
            assert request.method == "POST"
            return httpx.Response(
                200, json={"access_token": "fake-instagram-short", "user_id": "700"}
            )
        if request.url.host == "graph.instagram.com" and path == "/access_token":
            return httpx.Response(
                200, json={"access_token": "fake-instagram-long", "expires_in": 5_184_000}
            )
        if request.url.host == "graph.instagram.com" and path.endswith("/me"):
            return httpx.Response(200, json={"user_id": "700", "username": "@consultorio.ficticio"})
        if path.endswith("/oauth/access_token"):
            return httpx.Response(200, json={"access_token": "fake-meta-user"})
        if path.endswith("/me/accounts"):
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "500",
                            "name": options.get("name", "Consultorio ficticio"),
                            "access_token": "fake-page-grant",
                            "tasks": ["CREATE_CONTENT", "MESSAGING"],
                        }
                    ]
                },
            )
        if path.endswith("/600/phone_numbers"):
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "800",
                            "display_phone_number": "+15550001111",
                            "verified_name": "Consultorio ficticio",
                        }
                    ]
                },
            )
        if path.endswith("/subscribed_apps"):
            assert request.method == "POST"
            return httpx.Response(200, json={"success": True})
        raise AssertionError(f"unexpected provider path: {path}")

    orch = Orchestrator(settings, catalog=catalog, llm=FakeLLM())
    orch.social_login.provider.transport = httpx.MockTransport(provider)
    with TestClient(create_app(settings, orch), base_url="https://testserver") as client:
        yield client, orch, options


def begin(
    client: TestClient, provider: str, headers: dict[str, str] | None = None
) -> tuple[str, str]:
    response = client.post(f"/v1/social/login/{provider}/start", json={}, headers=headers or ACME)
    assert response.status_code == 200, response.text
    data = response.json()
    if provider == "whatsapp":
        flow_id, state = urlsplit(data["authorization_url"]).fragment.split(".")
        assert flow_id == data["flow_id"]
    else:
        state = parse_qs(urlsplit(data["authorization_url"]).query)["state"][0]
    assert "HttpOnly" in response.headers["set-cookie"]
    assert "SameSite=lax" in response.headers["set-cookie"]
    return data["flow_id"], state


def finish(client: TestClient, provider: str, state: str, flow_id: str) -> None:
    if provider == "whatsapp":
        headers = {"Origin": "https://testserver"}
        body = {"flow_id": flow_id, "state": state}
        config = client.post("/v1/social/login/whatsapp/launch", json=body, headers=headers)
        assert config.json() == {"app_id": "100", "config_id": "300", "version": "v26.0"}
        response = client.post(
            "/v1/social/login/whatsapp/complete",
            json={**body, "code": "synthetic-code"},
            headers=headers,
        )
        assert response.status_code == 200, response.text
    else:
        response = client.get(
            f"/v1/social/login/{provider}/callback",
            params={"state": state, "code": "synthetic-code"},
            follow_redirects=False,
        )
        assert response.status_code == 303, response.text
        assert response.headers["location"] == "/app#ajustes/redes"


def choose(
    client: TestClient, flow_id: str, headers: dict[str, str] | None = None
) -> httpx.Response:
    auth = headers or ACME
    pending = client.get(f"/v1/social/login/flows/{flow_id}", headers=auth).json()
    assert pending["status"] == "ready", pending
    assert "token" not in json.dumps(pending)
    return client.post(
        f"/v1/social/login/flows/{flow_id}/connect",
        json={"choice": pending["choices"][0]["choice"]},
        headers=auth,
    )


async def stored(orch: Orchestrator) -> tuple[list[Any], list[Any]]:
    async with orch.db.engine.connect() as conn:
        return (await conn.execute(select(credentials))).mappings().all(), (
            await conn.execute(select(flows))
        ).mappings().all()


@pytest.mark.parametrize(
    "provider,expected",
    [
        ("facebook", "fake-page-grant"),
        ("instagram", "fake-instagram-long"),
        ("whatsapp", "fake-meta-user"),
    ],
)
def test_login_discovers_assets_without_user_supplied_ids_and_seals_the_grant(
    login: tuple[TestClient, Orchestrator, dict[str, Any]],
    provider: str,
    expected: str,
) -> None:
    client, orch, _ = login
    fid, state = begin(client, provider)
    finish(client, provider, state, fid)
    response = choose(client, fid)
    assert response.status_code == 200, response.text
    account = response.json()
    assert account["network"] == provider and account["secret_configured"]
    assert expected not in response.text
    assert client.portal is not None
    assert (
        client.portal.call(
            orch.social.credentials.resolve,
            "acme",
            account["secret_ref"],
            account["network"],
            account["external_id"],
        )
        == expected
    )
    rows, attempts = client.portal.call(stored, orch)
    assert expected not in str(rows) and expected not in str(attempts)
    assert state not in str(attempts)
    assert attempts[0]["payload_sealed"] is None
    assert expected not in client.get("/v1/audit", headers=ACME).text
    assert (
        client.delete("/v1/social/accounts/" + account["account_id"], headers=ACME).status_code
        == 204
    )
    assert client.portal.call(stored, orch)[0] == []
    with pytest.raises(ReconnectRequired):
        client.portal.call(
            orch.social.credentials.resolve,
            "acme",
            account["secret_ref"],
            account["network"],
            account["external_id"],
        )


def test_state_is_single_use_and_bound_to_browser_and_provider(
    login: tuple[TestClient, Orchestrator, dict[str, Any]],
) -> None:
    client, _, options = login
    fid, state = begin(client, "facebook")
    url = "/v1/social/login/facebook/callback"
    params = {"state": state, "code": "synthetic-code"}
    assert (
        client.get(
            url, params=params, headers={"Cookie": "tatos_social_binding=" + "f" * 64}
        ).status_code
        == 404
    )
    assert not options["calls"]
    assert client.get("/v1/social/login/instagram/callback", params=params).status_code == 404
    finish(client, "facebook", state, fid)
    count = len(options["calls"])
    assert client.get(url, params=params).status_code == 404
    assert len(options["calls"]) == count


def test_a_different_tenant_or_staff_member_cannot_finish_a_flow(
    login: tuple[TestClient, Orchestrator, dict[str, Any]],
) -> None:
    client, _, _ = login
    fid, state = begin(client, "facebook")
    finish(client, "facebook", state, fid)
    staff = client.post(
        "/v1/admin/staff", json={"name": "otra.persona", "roles": ["marketing"]}, headers=ACME
    ).json()
    for auth in [GLOBEX, {"X-API-Key": staff["key"]}]:
        assert client.get(f"/v1/social/login/flows/{fid}", headers=auth).status_code == 404
        assert (
            client.post(
                f"/v1/social/login/flows/{fid}/connect", json={"choice": "a" * 32}, headers=auth
            ).status_code
            == 404
        )
    assert choose(client, fid).status_code == 200


@pytest.mark.parametrize(
    "change,error",
    [({"scopes": []}, "permissions_missing"), ({"app_id": "999"}, "wrong_application")],
)
def test_provider_grant_must_belong_to_tatos_and_have_required_permissions(
    login: tuple[TestClient, Orchestrator, dict[str, Any]],
    change: dict[str, Any],
    error: str,
) -> None:
    client, _, options = login
    options.update(change)
    fid, state = begin(client, "facebook")
    finish(client, "facebook", state, fid)
    pending = client.get(f"/v1/social/login/flows/{fid}", headers=ACME).json()
    assert pending["status"] == "failed" and pending["error_code"] == error
    assert pending["choices"] == []
    assert client.get("/v1/social/accounts", headers=ACME).json() == []


def test_unauthorized_origin_cannot_finish_whatsapp(
    login: tuple[TestClient, Orchestrator, dict[str, Any]],
) -> None:
    client, _, options = login
    fid, state = begin(client, "whatsapp")
    body = {"flow_id": fid, "state": state, "code": "synthetic-code"}
    assert (
        client.post(
            "/v1/social/login/whatsapp/complete",
            json=body,
            headers={"Origin": "https://attacker.invalid"},
        ).status_code
        == 403
    )
    assert not options["calls"]
    finish(client, "whatsapp", state, fid)


def test_cancelled_login_and_expired_flow_never_connect(
    login: tuple[TestClient, Orchestrator, dict[str, Any]],
) -> None:
    client, orch, _ = login
    fid, state = begin(client, "facebook")
    r = client.get(
        "/v1/social/login/facebook/callback",
        params={"state": state, "error": "access_denied"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert (
        client.get(f"/v1/social/login/flows/{fid}", headers=ACME).json()["error_code"]
        == "authorization_cancelled"
    )

    async def expire() -> None:
        async with orch.db.engine.begin() as conn:
            await conn.execute(update(flows).values(expires_at=utcnow() - timedelta(seconds=1)))

    assert client.portal is not None
    client.portal.call(expire)
    assert client.get(f"/v1/social/login/flows/{fid}", headers=ACME).status_code == 404
    assert client.portal.call(orch.social_login.purge) == 1


def test_same_asset_cannot_be_connected_to_two_clinics(
    login: tuple[TestClient, Orchestrator, dict[str, Any]],
) -> None:
    client, _, _ = login
    fid, state = begin(client, "facebook")
    finish(client, "facebook", state, fid)
    assert choose(client, fid).status_code == 200
    other, state = begin(client, "facebook", GLOBEX)
    finish(client, "facebook", state, other)
    assert choose(client, other, GLOBEX).status_code == 409
    assert client.get("/v1/social/accounts", headers=GLOBEX).json() == []


def test_marketing_can_connect_and_reception_cannot(
    login: tuple[TestClient, Orchestrator, dict[str, Any]],
) -> None:
    client, _, _ = login
    for role, allowed in [
        ("marketing", True),
        ("owner", True),
        ("reception", False),
        ("reviewer", False),
    ]:
        staff = client.post(
            "/v1/admin/staff", json={"name": role, "roles": [role]}, headers=ACME
        ).json()
        auth = {"X-API-Key": staff["key"]}
        assert client.get("/v1/social/login/configuration", headers=auth).status_code == (
            200 if allowed else 403
        )


def test_a_token_expired_at_rest_requires_reconnection_instead_of_a_successful_dry_run(
    login: tuple[TestClient, Orchestrator, dict[str, Any]],
) -> None:
    client, orch, _ = login
    fid, state = begin(client, "instagram")
    finish(client, "instagram", state, fid)
    account = choose(client, fid).json()

    async def expire() -> None:
        async with orch.db.engine.begin() as conn:
            await conn.execute(
                update(credentials).values(expires_at=utcnow() - timedelta(seconds=1))
            )

    assert client.portal is not None
    client.portal.call(expire)
    with pytest.raises(ReconnectRequired):
        client.portal.call(
            orch.social.credentials.resolve,
            "acme",
            account["secret_ref"],
            account["network"],
            account["external_id"],
        )
    assert not client.get("/v1/social/accounts", headers=ACME).json()[0]["secret_configured"]
    assert client.portal.call(
        orch.inbox._deliver, "acme", "instagram", account["account_id"], "sender", "hello"
    ) == ("failed", None)


def test_oauth_query_secrets_are_redacted_from_logs(caplog: pytest.LogCaptureFixture) -> None:
    text = "GET /callback?code=synthetic-private-code&state=synthetic-private-state"
    assert "synthetic-private-code" not in _scrub(text)
    with caplog.at_level(logging.WARNING):
        logging.getLogger("httpx").warning(
            "GET https://graph.facebook.com/oauth?client_secret=synthetic-secret&fb_exchange_token=synthetic-token"
        )
    assert "synthetic-secret" not in caplog.text and "synthetic-token" not in caplog.text


@pytest.mark.parametrize(
    "parameter", ["hub.verify_token", "hub%2Everify_token", "verify_token", "code", "state"]
)
def test_webhook_verification_secret_is_hidden_without_breaking_uvicorn(parameter: str) -> None:
    from uvicorn.logging import AccessFormatter

    path = (
        "/v1/channels/meta?hub.mode=subscribe&"
        f"{parameter}=fake-webhook-private&hub.challenge=challenge"
    )
    record = logging.getLogger("uvicorn.access").makeRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:1234", "GET", path, "1.1", 200),
        None,
    )
    rendered = AccessFormatter(fmt="%(message)s", use_colors=False).format(record)
    assert "fake-webhook-private" not in rendered
    assert "challenge" in rendered and "200" in rendered
    assert isinstance(record.args, tuple) and len(record.args) == 5


def test_sealed_grants_cannot_be_swapped_between_accounts(
    login: tuple[TestClient, Orchestrator, dict[str, Any]],
) -> None:
    client, orch, _ = login
    accounts = []
    for provider in ["facebook", "instagram"]:
        fid, state = begin(client, provider)
        finish(client, provider, state, fid)
        accounts.append(choose(client, fid).json())

    async def swap() -> None:
        async with orch.db.engine.begin() as conn:
            rows = (await conn.execute(select(credentials))).mappings().all()
            for source, destination in [(rows[0], rows[1]), (rows[1], rows[0])]:
                await conn.execute(
                    update(credentials)
                    .where(credentials.c.secret_ref == destination["secret_ref"])
                    .values(token_sealed=source["token_sealed"])
                )

    assert client.portal is not None
    client.portal.call(swap)
    for account in accounts:
        with pytest.raises(ReconnectRequired):
            client.portal.call(
                orch.social.credentials.resolve,
                "acme",
                account["secret_ref"],
                account["network"],
                account["external_id"],
            )
        with pytest.raises(ReconnectRequired):
            client.portal.call(
                orch.social.credentials.resolve,
                "globex",
                account["secret_ref"],
                account["network"],
                account["external_id"],
            )


def test_revoked_staff_cannot_finish_their_pending_connection(
    login: tuple[TestClient, Orchestrator, dict[str, Any]],
) -> None:
    client, _, _ = login
    created = client.post(
        "/v1/admin/staff", json={"name": "marketer", "roles": ["marketing"]}, headers=ACME
    )
    assert created.status_code == 201
    staff = created.json()
    headers = {"X-API-Key": staff["key"]}
    fid, state = begin(client, "facebook", headers)
    finish(client, "facebook", state, fid)
    pending = client.get(f"/v1/social/login/flows/{fid}", headers=headers).json()
    assert client.delete("/v1/admin/principals/" + staff["id"], headers=ACME).status_code == 204
    assert (
        client.post(
            f"/v1/social/login/flows/{fid}/connect",
            json={"choice": pending["choices"][0]["choice"]},
            headers=headers,
        ).status_code
        == 401
    )
    assert client.get("/v1/social/accounts", headers=ACME).json() == []


def test_disconnected_oauth_account_cannot_be_recorded_as_a_published_dry_run(
    login: tuple[TestClient, Orchestrator, dict[str, Any]],
) -> None:
    client, orch, _ = login
    fid, state = begin(client, "facebook")
    finish(client, "facebook", state, fid)
    account = choose(client, fid).json()
    assert (
        client.delete("/v1/social/accounts/" + account["account_id"], headers=ACME).status_code
        == 204
    )
    assert client.portal is not None
    outcome, details = client.portal.call(
        orch.publications._send, "acme", {"account_id": account["account_id"]}
    )
    assert outcome == "failed" and details["mode"] == "live"


def test_reusing_an_oauth_reference_on_another_account_never_uses_its_token(
    login: tuple[TestClient, Orchestrator, dict[str, Any]],
) -> None:
    client, orch, _ = login
    fid, state = begin(client, "facebook")
    finish(client, "facebook", state, fid)
    account = choose(client, fid).json()

    async def alias() -> None:
        async with orch.db.engine.begin() as conn:
            await conn.execute(
                insert(channel_accounts).values(
                    tenant="acme",
                    account_id="other-account",
                    network="facebook",
                    external_id="999",
                    handle="Alias incorrecto",
                    secret_ref=account["secret_ref"],
                    audited=False,
                    active=True,
                    created_at=utcnow(),
                    created_by="synthetic",
                )
            )

    def no_delivery(request: httpx.Request) -> httpx.Response:
        raise AssertionError("wrong account must never receive the token")

    orch.inbox.transport = httpx.MockTransport(no_delivery)
    assert client.portal is not None
    client.portal.call(alias)
    assert client.portal.call(
        orch.inbox._deliver, "acme", "facebook", "other-account", "sender", "hello"
    ) == ("failed", None)
    assert not client.get("/v1/social/accounts", headers=ACME).json()[1]["secret_configured"]


def test_instagram_product_secret_cannot_sign_other_products(
    login: tuple[TestClient, Orchestrator, dict[str, Any]],
) -> None:
    client, orch, _ = login
    orch.settings.whatsapp_verify_token = SecretStr("synthetic-webhook-verification")
    for product, secret, status in [
        ("instagram", "synthetic-instagram-secret", 200),
        ("instagram", "synthetic-app-secret", 401),
        ("whatsapp_business_account", "synthetic-instagram-secret", 401),
        ("whatsapp_business_account", "synthetic-app-secret", 200),
    ]:
        body = json.dumps({"object": product, "entry": []}).encode()
        signature = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        response = client.post(
            "/v1/channels/meta",
            content=body,
            headers={"X-Hub-Signature-256": signature, "Content-Type": "application/json"},
        )
        assert response.status_code == status
