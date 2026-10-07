"""Social channels (ADR 0015): platform rules with their sources, pre-flight checks, and
accounts per establishment or professional whose tokens never pass through the API."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from orchestrator.api.app import create_app
from orchestrator.catalog import Catalog
from orchestrator.config import Settings
from orchestrator.llm import FakeLLM
from orchestrator.service import Orchestrator
from orchestrator.social import (
    NETWORKS,
    PLATFORM_RULES,
    check_publish,
    check_whatsapp_message,
    network_verified,
    resolve_secret,
    rules_for,
)

ACME = {"X-API-Key": "test-key"}
GLOBEX = {"X-API-Key": "other-key"}
TOKEN = "EAAG-very-secret-token-value"


# --- rules ------------------------------------------------------------------------------


def test_every_read_rule_cites_its_official_page() -> None:
    for rule in PLATFORM_RULES:
        if rule.status == "read":
            assert rule.source.startswith("https://"), rule.id
    assert {r.network for r in PLATFORM_RULES} >= {"whatsapp", "instagram", "tiktok", "facebook"}


def test_facebook_is_not_verified_and_cannot_publish() -> None:
    assert not network_verified("facebook")
    check = check_publish("facebook", media_type="image", media_format="jpg", public_url=True)
    assert not check.allowed and "not verified" in check.problems[0]


def test_messaging_networks_do_not_publish() -> None:
    for network in ("whatsapp", "telegram"):
        assert not check_publish(network, media_type="text").allowed


# --- pre-flight checks ------------------------------------------------------------------


def test_instagram_accepts_a_public_jpeg() -> None:
    ok = check_publish("instagram", media_type="image", media_format=".JPG", public_url=True)
    assert ok.allowed and ok.visibility == "public"


@pytest.mark.parametrize(
    ("kwargs", "rule"),
    [
        ({"media_type": "image", "media_format": "png", "public_url": True}, "IG-JPEG"),
        ({"media_type": "image", "media_format": "jpg", "public_url": False}, "IG-PUBLIC-URL"),
        ({"media_type": "text", "public_url": True}, "IG-JPEG"),
        (
            {
                "media_type": "image",
                "media_format": "jpg",
                "public_url": True,
                "posts_last_24h": 100,
            },
            "IG-LIMIT",
        ),
    ],
)
def test_instagram_refuses_what_the_platform_refuses(kwargs: dict[str, object], rule: str) -> None:
    check = check_publish("instagram", **kwargs)  # type: ignore[arg-type]
    assert not check.allowed and any(rule in p for p in check.problems)


def test_tiktok_posts_stay_private_until_the_client_is_audited() -> None:
    unaudited = check_publish("tiktok", media_type="video", media_format="mp4")
    assert unaudited.allowed and unaudited.visibility == "private"
    audited = check_publish("tiktok", media_type="video", media_format="mp4", audited=True)
    assert audited.visibility == "public"
    assert not check_publish("tiktok", media_type="video", media_format="mov").allowed
    assert not check_publish("tiktok", media_type="image", public_url=False).allowed


def test_whatsapp_needs_opt_in_a_template_outside_the_window_and_a_human() -> None:
    ok = {"opted_in": True, "window_open": True, "template_status": None, "human_handoff": True}
    assert check_whatsapp_message(**ok) == []  # type: ignore[arg-type]
    assert (
        check_whatsapp_message(**{**ok, "window_open": False, "template_status": "APPROVED"}) == []
    )  # type: ignore[arg-type]
    for change, rule in [
        ({"opted_in": False}, "WA-OPT-IN"),
        ({"window_open": False}, "WA-WINDOW"),
        ({"window_open": False, "template_status": "PENDING"}, "WA-WINDOW"),
        ({"human_handoff": False}, "WA-HUMAN"),
    ]:
        problems = check_whatsapp_message(**{**ok, **change})  # type: ignore[arg-type]
        assert any(rule in p for p in problems), change


def test_rules_for_each_network() -> None:
    assert all(rules_for(n) or n == "telegram" for n in NETWORKS)


# --- secrets ----------------------------------------------------------------------------


def test_secrets_come_from_the_environment_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SOCIAL_SECRET_WA_MAIN", TOKEN)
    assert resolve_secret("WA_MAIN") == TOKEN
    assert resolve_secret("NOT_SET") is None
    assert resolve_secret("../etc") is None  # not a valid reference name


# --- accounts over HTTP -----------------------------------------------------------------


@pytest.fixture
def client(settings: Settings, catalog: Catalog) -> Iterator[TestClient]:
    orch = Orchestrator(settings, catalog=catalog, llm=FakeLLM())
    with TestClient(create_app(settings, orch)) as c:
        # Accounts given to a professional must name a registered one.
        for headers in (ACME, GLOBEX):
            professional = {
                "professional_id": "dr-demo",
                "display_name": "Dr Demo",
                "pack_id": "general",
            }
            assert (
                c.post("/v1/admin/professionals", json=professional, headers=headers).status_code
                == 201
            )
        yield c


def account(**overrides: object) -> dict[str, object]:
    return {
        "network": "instagram",
        "external_id": "17841400000000000",
        "handle": "@consultorio.demo",
        "secret_ref": "IG_DR_DEMO",
        "professional_id": "dr-demo",
        **overrides,
    }


def test_connect_list_and_disable_an_account(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    created = client.post("/v1/social/accounts", json=account(), headers=ACME)
    assert created.status_code == 201
    body = created.json()
    assert body["secret_configured"] is False and body["active"] is True
    monkeypatch.setenv("SOCIAL_SECRET_IG_DR_DEMO", TOKEN)
    listed = client.get("/v1/social/accounts", headers=ACME)
    assert listed.json()[0]["secret_configured"] is True
    assert TOKEN not in listed.text  # the token never leaves the server
    mine = client.get("/v1/social/accounts?professional_id=dr-demo", headers=ACME).json()
    others = client.get("/v1/social/accounts?professional_id=dr-other", headers=ACME).json()
    assert len(mine) == 1 and others == []

    account_id = body["account_id"]
    assert client.delete(f"/v1/social/accounts/{account_id}", headers=ACME).status_code == 204
    assert client.delete(f"/v1/social/accounts/{account_id}", headers=ACME).status_code == 404
    assert client.get("/v1/social/accounts", headers=ACME).json()[0]["active"] is False

    audit = client.get("/v1/audit", headers=ACME).text
    assert "channel_account.connected" in audit and "channel_account.disabled" in audit
    assert TOKEN not in audit


def test_the_same_account_cannot_be_connected_twice(client: TestClient) -> None:
    assert client.post("/v1/social/accounts", json=account(), headers=ACME).status_code == 201
    assert client.post("/v1/social/accounts", json=account(), headers=ACME).status_code == 409
    # Another tenant may connect its own record of the same id; tenants never collide.
    assert client.post("/v1/social/accounts", json=account(), headers=GLOBEX).status_code == 201


def test_tenants_are_isolated(client: TestClient) -> None:
    created = client.post("/v1/social/accounts", json=account(), headers=ACME).json()
    assert client.get("/v1/social/accounts", headers=GLOBEX).json() == []
    delete = client.delete(f"/v1/social/accounts/{created['account_id']}", headers=GLOBEX)
    assert delete.status_code == 404


@pytest.mark.parametrize(
    "bad",
    [
        {"secret_ref": "EAAG-very-secret-token-value"},  # a token pasted instead of a name
        {"secret_ref": "lowercase"},
        {"network": "myspace"},
        {"external_id": "has spaces"},
    ],
)
def test_bad_input_is_rejected(client: TestClient, bad: dict[str, object]) -> None:
    assert client.post("/v1/social/accounts", json=account(**bad), headers=ACME).status_code == 422


def test_only_admins_connect_accounts(client: TestClient) -> None:
    staff = client.post(
        "/v1/admin/staff", json={"name": "Recepción", "roles": ["reception"]}, headers=ACME
    ).json()
    reception = {"X-API-Key": staff["key"]}
    assert client.post("/v1/social/accounts", json=account(), headers=reception).status_code == 403
    assert client.get("/v1/social/accounts", headers=reception).status_code == 200
    rules = client.get("/v1/social/rules", headers=reception).json()
    assert rules["networks"]["facebook"]["verified"] is False
    assert rules["networks"]["instagram"]["verified"] is True
