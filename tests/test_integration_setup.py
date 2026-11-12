"""Onboarding references cannot authorize Meta or trigger an undocumented vendor API."""

from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from orchestrator.api.app import create_app
from orchestrator.catalog import Catalog
from orchestrator.config import Settings
from orchestrator.governance import audit_events
from orchestrator.integration_setup import profiles
from orchestrator.llm import FakeLLM
from orchestrator.sealing import SealError
from orchestrator.service import Orchestrator

ADMIN = {"X-API-Key": "test-key"}
OTHER = {"X-API-Key": "other-key"}
BASE = "/v1/integration-setup"
META = {"ruc": "0000000000001", "account_id": "123456789000"}


@pytest.fixture
def setup_api(settings: Settings, catalog: Catalog) -> Iterator[tuple[TestClient, Orchestrator]]:
    orch = Orchestrator(settings, catalog=catalog, llm=FakeLLM())
    with TestClient(create_app(settings, orch)) as client:
        yield client, orch


def test_references_stay_tenant_scoped_encrypted_and_out_of_audit(
    setup_api: tuple[TestClient, Orchestrator],
) -> None:
    client, orch = setup_api
    before = client.get("/v1/social/login/configuration", headers=ADMIN).json()
    saved = client.put(BASE + "/meta", headers=ADMIN, json={"version": 0, "profile": META})
    assert saved.status_code == 200
    assert saved.json()["profile"] == META
    assert saved.json()["identity_verified"] is False
    assert saved.json()["external_connection"] == "not_checked"
    assert client.get("/v1/social/login/configuration", headers=ADMIN).json() == before
    other = client.get(BASE, headers=OTHER)
    assert all(entry["profile"] is None for entry in other.json())
    assert other.headers["cache-control"] == "no-store"

    async def stored() -> tuple[Any, list[Any]]:
        async with orch.db.engine.connect() as conn:
            profile = (await conn.execute(select(profiles))).mappings().one()
            events = (await conn.execute(select(audit_events))).mappings().all()
        return profile, events

    assert client.portal is not None
    row, events = client.portal.call(stored)
    assert row["profile_sealed"].startswith("v1:")
    assert META["ruc"] not in row["profile_sealed"]
    assert META["account_id"] not in row["profile_sealed"]
    assert META["ruc"] not in str(events) and META["account_id"] not in str(events)
    assert len([e for e in events if e["action"] == "integration.references.saved"]) == 1
    for binding in ("globex/meta", "acme/tufacturero"):
        with pytest.raises(SealError):
            orch.integration_setup.sealer.open(binding, row["profile_sealed"])


def test_stale_update_and_clear_cannot_overwrite_another_revision(
    setup_api: tuple[TestClient, Orchestrator],
) -> None:
    client, _ = setup_api
    body = {"version": 0, "profile": META}
    assert client.put(BASE + "/meta", headers=ADMIN, json=body).status_code == 200
    assert client.put(BASE + "/meta", headers=ADMIN, json=body).status_code == 409
    assert client.delete(BASE + "/meta?version=0", headers=ADMIN).status_code == 409
    cleared = client.delete(BASE + "/meta?version=1", headers=ADMIN)
    assert cleared.status_code == 200 and cleared.json()["version"] == 2
    assert cleared.json()["profile"] is None and cleared.json()["configured"] is False
    assert client.put(BASE + "/meta", headers=ADMIN, json=body).status_code == 409
    assert client.delete(BASE + "/meta?version=1", headers=ADMIN).status_code == 409
    body["version"] = 2
    assert client.put(BASE + "/meta", headers=ADMIN, json=body).json()["version"] == 3
    assert all(entry["version"] == 0 for entry in client.get(BASE, headers=OTHER).json())


@pytest.mark.parametrize("role", ["reception", "marketing", "reviewer", "privacy"])
def test_other_roles_cannot_read_write_or_clear_business_references(
    setup_api: tuple[TestClient, Orchestrator], role: str
) -> None:
    client, _ = setup_api
    member = client.post(
        "/v1/admin/staff", headers=ADMIN, json={"name": "Synthetic.QA", "roles": [role]}
    )
    assert member.status_code == 201
    headers = {"X-API-Key": member.json()["key"]}
    assert client.get(BASE, headers=headers).status_code == 403
    assert (
        client.put(
            BASE + "/meta", headers=headers, json={"version": 0, "profile": META}
        ).status_code
        == 403
    )
    assert client.delete(BASE + "/meta?version=0", headers=headers).status_code == 403


def test_owner_has_access_but_patient_and_anonymous_do_not(
    setup_api: tuple[TestClient, Orchestrator],
) -> None:
    client, _ = setup_api
    member = client.post(
        "/v1/admin/staff", headers=ADMIN, json={"name": "Synthetic.owner", "roles": ["owner"]}
    ).json()
    assert client.get(BASE, headers={"X-API-Key": member["key"]}).status_code == 200
    assert client.get(BASE).status_code == 401
    client.post(
        "/v1/crm/patients",
        headers=ADMIN,
        json={"id": "p-setup-qa", "display_name": "Synthetic patient"},
    )
    access = client.post("/v1/crm/patients/p-setup-qa/access", headers=ADMIN).json()
    assert client.get(BASE, headers={"X-API-Key": access["key"]}).status_code == 403


@pytest.mark.parametrize(
    "provider,profile",
    [
        ("meta", {"ruc": "short", "account_id": "123"}),
        ("meta", {"ruc": META["ruc"], "account_id": "not-a-business-id"}),
        ("meta", {"ruc": META["ruc"]}),
        ("tufacturero", {"account_id": "demo"}),
        ("mentaltestlab", {"ruc": META["ruc"], "account_id": "demo"}),
        ("mentaltestlab", {"account_id": "https://example.com/account"}),
        ("mentaltestlab", {}),
        ("tufacturero", {**META, "api_key": "synthetic-not-a-credential"}),
        ("tufacturero", {**META, "base_url": "http://127.0.0.1"}),
    ],
)
def test_rejects_wrong_identity_fields_secrets_and_arbitrary_urls(
    setup_api: tuple[TestClient, Orchestrator], provider: str, profile: dict[str, Any]
) -> None:
    client, _ = setup_api
    response = client.put(
        BASE + "/" + provider, headers=ADMIN, json={"version": 0, "profile": profile}
    )
    assert response.status_code == 422
    assert not any(entry["configured"] for entry in client.get(BASE, headers=ADMIN).json())


def test_vendor_setup_never_sends_network_requests_or_claims_connection(
    setup_api: tuple[TestClient, Orchestrator], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = setup_api

    async def no_external(*args: Any, **kwargs: Any) -> None:
        pytest.fail("preparation must not contact an external service")

    monkeypatch.setattr(httpx.AsyncClient, "request", no_external)
    for provider, profile in (
        ("tufacturero", {"ruc": META["ruc"]}),
        ("mentaltestlab", {"account_id": "fictitious-organization"}),
    ):
        response = client.put(
            BASE + "/" + provider, headers=ADMIN, json={"version": 0, "profile": profile}
        )
        assert response.status_code == 200
        assert response.json()["status"] == "references_saved"
        assert response.json()["api_transport_implemented"] is False
        assert response.json()["external_connection"] == "not_checked"
    assert (
        client.put(
            BASE + "/unknown", headers=ADMIN, json={"version": 0, "profile": META}
        ).status_code
        == 422
    )
