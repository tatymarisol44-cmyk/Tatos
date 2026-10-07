"""An establishment with several professionals, each with their own profession pack."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from orchestrator.api.app import create_app
from orchestrator.catalog import Catalog
from orchestrator.config import Settings
from orchestrator.llm import FakeLLM
from orchestrator.service import Orchestrator

ADMIN = {"X-API-Key": "test-key"}  # tenant acme, whose own pack is `general`
GLOBEX = {"X-API-Key": "other-key"}
BRIEF = {
    "title": "Tu bienestar importa",
    "points": ["Hablarlo ayuda."],
    "cta": "Agenda tu cita",
    "practice_name": "Centro Demo",
}


@pytest.fixture
def client(settings: Settings, catalog: Catalog, tmp_path: Any) -> Iterator[TestClient]:
    settings.media_dir = tmp_path / "media"
    orch = Orchestrator(settings, catalog=catalog, llm=FakeLLM())
    with TestClient(create_app(settings, orch)) as c:
        yield c


def add(client: TestClient, pid: str, pack: str, headers: dict[str, str] = ADMIN) -> Any:
    body = {"professional_id": pid, "display_name": pid.title(), "pack_id": pack}
    return client.post("/v1/admin/professionals", json=body, headers=headers)


def account(client: TestClient, professional_id: str | None, external_id: str) -> str:
    body = {
        "network": "instagram",
        "external_id": external_id,
        "handle": "@centro",
        "secret_ref": "IG_CENTRO",
        "professional_id": professional_id,
    }
    response = client.post("/v1/social/accounts", json=body, headers=ADMIN)
    assert response.status_code == 201, response.text
    return str(response.json()["account_id"])


def publish_draft(client: TestClient, account_id: str, caption: str) -> Any:
    body = {"account_id": account_id, "kind": "infographic", "caption": caption, "brief": BRIEF}
    return client.post("/v1/social/publications", json=body, headers=ADMIN)


def test_professionals_with_their_own_packs(client: TestClient) -> None:
    assert add(client, "dra.vera", "ec-psychologist").status_code == 201
    assert add(client, "dr.ruiz", "ec-psychiatrist").status_code == 201
    listed = {
        p["professional_id"]: p["pack_id"]
        for p in client.get("/v1/admin/professionals", headers=ADMIN).json()
    }
    assert listed == {"dra.vera": "ec-psychologist", "dr.ruiz": "ec-psychiatrist"}
    assert client.get("/v1/admin/professionals", headers=GLOBEX).json() == []
    assert "professional.added" in client.get("/v1/audit", headers=ADMIN).text


@pytest.mark.parametrize("pack", ["nope", "ec-mental-health-base"])
def test_unknown_or_abstract_packs_are_refused(client: TestClient, pack: str) -> None:
    assert add(client, "dra.vera", pack).status_code == 409


def test_duplicates_and_disabling(client: TestClient) -> None:
    add(client, "dra.vera", "ec-psychologist")
    assert add(client, "dra.vera", "ec-psychologist").status_code == 409
    assert client.delete("/v1/admin/professionals/dra.vera", headers=ADMIN).status_code == 204
    assert client.delete("/v1/admin/professionals/dra.vera", headers=ADMIN).status_code == 404
    assert client.delete("/v1/admin/professionals/dra.vera", headers=GLOBEX).status_code == 404


def test_accounts_must_name_a_registered_active_professional(client: TestClient) -> None:
    body = {
        "network": "instagram",
        "external_id": "1",
        "handle": "@x",
        "secret_ref": "IG_X",
        "professional_id": "ghost",
    }
    assert client.post("/v1/social/accounts", json=body, headers=ADMIN).status_code == 422
    add(client, "dra.vera", "ec-psychologist")
    client.delete("/v1/admin/professionals/dra.vera", headers=ADMIN)
    body["professional_id"] = "dra.vera"
    assert client.post("/v1/social/accounts", json=body, headers=ADMIN).status_code == 422


def test_marketing_follows_the_professionals_pack_not_the_establishments(
    client: TestClient,
) -> None:
    """The establishment is on `general`; its psychologist's posts obey the mental-health
    rules, and the practice's own account obeys the establishment's."""
    add(client, "dra.vera", "ec-psychologist")
    psychologist = account(client, "dra.vera", "ig-vera")
    practice = account(client, None, "ig-centro")
    promise = "Curación en pocas sesiones"  # banned by the mental-health pack only
    rejected = publish_draft(client, psychologist, promise)
    assert rejected.status_code == 422
    assert "banned_claim:curación" in rejected.json()["detail"]["violations"]
    assert publish_draft(client, practice, promise).status_code == 201  # general pack


def test_a_disabled_professional_cannot_publish(client: TestClient) -> None:
    add(client, "dra.vera", "ec-psychologist")
    psychologist = account(client, "dra.vera", "ig-vera")
    client.delete("/v1/admin/professionals/dra.vera", headers=ADMIN)
    assert publish_draft(client, psychologist, "Tu bienestar importa.").status_code == 409


def test_only_admins_manage_professionals(client: TestClient) -> None:
    staff = client.post(
        "/v1/admin/staff", json={"name": "recepcion", "roles": ["reception"]}, headers=ADMIN
    ).json()
    reception = {"X-API-Key": staff["key"]}
    assert add(client, "dra.vera", "ec-psychologist", reception).status_code == 403
    assert client.get("/v1/admin/professionals", headers=reception).status_code == 200
