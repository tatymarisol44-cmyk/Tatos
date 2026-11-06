"""Manual campaign audiences: consent, deduplication, tenant boundaries and editing."""

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from orchestrator.api.app import create_app
from orchestrator.config import Settings
from orchestrator.llm import FakeLLM
from orchestrator.service import Orchestrator

ADMIN = {"X-API-Key": "test-key"}
OTHER = {"X-API-Key": "other-key"}
GOOD = "Hola {first_name}, tenemos novedades. Responde STOP para no recibir más mensajes."


def ok(response: Any) -> Any:
    assert response.status_code < 300, response.text
    return response.json()


@pytest.fixture
def clinic(settings: Settings) -> Iterator[TestClient]:
    settings.campaign_default_holdout_pct = 0
    orch = Orchestrator(settings, llm=FakeLLM())
    with TestClient(create_app(settings, orch)) as client:
        for pid, name in [
            ("ana", "Ana Ficticia"),
            ("bea", "Bea Ficticia"),
            ("carla", "Carla Ficticia"),
            ("dora", "Dora Ficticia"),
        ]:
            ok(
                client.post(
                    "/v1/crm/patients",
                    headers=ADMIN,
                    json={
                        "id": pid,
                        "display_name": name,
                        "phone": "0999999999",
                        "telegram_chat_id": "123456",
                    },
                )
            )
            if pid != "carla":
                ok(
                    client.put(
                        f"/v1/subjects/{pid}/consents/marketing",
                        headers=ADMIN,
                        json={"granted": True, "source": "form"},
                    )
                )
        yield client


def group(client: TestClient, name: str, members: list[str]) -> dict[str, Any]:
    return ok(
        client.post(
            "/v1/marketing/groups", headers=ADMIN, json={"name": name, "member_ids": members}
        )
    )


def campaign(client: TestClient, ids: list[str], name: str = "Ejemplo") -> dict[str, Any]:
    return ok(
        client.post(
            "/v1/campaigns",
            headers=ADMIN,
            json={
                "name": name,
                "kind": "education",
                "segment": "manual_groups",
                "group_ids": ids,
                "template": GOOD,
                "mode": "simulation",
                "holdout_pct": 0,
            },
        )
    )


def test_membership_is_persistent_exclusive_and_has_no_clinical_or_contact_details(
    clinic: TestClient,
) -> None:
    a = group(clinic, "Taller A", ["ana"])
    b = group(clinic, "Taller B", ["bea", "carla"])
    assert ok(clinic.get(f"/v1/marketing/groups/{a['id']}", headers=ADMIN))["member_ids"] == [
        "ana",
    ]
    rows = ok(clinic.get("/v1/marketing/contacts", headers=ADMIN))["contacts"]
    bea = next(r for r in rows if r["id"] == "bea")
    assert bea["group_ids"] == [b["id"]]
    assert bea["marketing_consent"] is True
    assert set(bea) == {"id", "display_name", "group_ids", "marketing_consent"}
    carla = next(r for r in rows if r["id"] == "carla")
    assert carla["marketing_consent"] is False
    page = ok(clinic.get("/v1/marketing/contacts?limit=2&offset=2", headers=ADMIN))
    assert page["total"] == 4 and len(page["contacts"]) == 2
    found = ok(clinic.get("/v1/marketing/contacts?search=Bea", headers=ADMIN))
    assert [p["id"] for p in found["contacts"]] == ["bea"]


def test_edits_require_current_version_and_delete_group_keeps_people(clinic: TestClient) -> None:
    a = group(clinic, "Grupo A", ["ana"])
    path = f"/v1/marketing/groups/{a['id']}"
    saved = ok(
        clinic.put(
            path,
            headers=ADMIN,
            json={
                "name": "Grupo nuevo",
                "member_ids": ["bea"],
                "version": a["version"],
            },
        )
    )
    assert saved["member_ids"] == ["bea"] and saved["version"] == 2
    assert (
        clinic.put(
            path,
            headers=ADMIN,
            json={
                "name": "Cambio obsoleto",
                "member_ids": ["ana"],
                "version": 1,
            },
        ).status_code
        == 409
    )
    assert clinic.delete(path + "?version=1", headers=ADMIN).status_code == 409
    ok(clinic.delete(path + "?version=2", headers=ADMIN))
    assert clinic.get(path, headers=ADMIN).status_code == 404
    assert ok(clinic.get("/v1/crm/patients/bea", headers=ADMIN))["display_name"] == "Bea Ficticia"


def test_names_and_missing_or_foreign_people_are_rejected_without_partial_changes(
    clinic: TestClient,
) -> None:
    a = group(clinic, "Grupo A", ["ana"])
    assert (
        clinic.post(
            "/v1/marketing/groups",
            headers=ADMIN,
            json={
                "name": " grupo a ",
            },
        ).status_code
        == 409
    )
    assert (
        clinic.post(
            "/v1/marketing/groups",
            headers=ADMIN,
            json={
                "name": "   ",
            },
        ).status_code
        == 422
    )
    assert (
        clinic.post(
            "/v1/marketing/groups",
            headers=OTHER,
            json={
                "name": "Otro",
                "member_ids": ["ana"],
            },
        ).status_code
        == 422
    )
    assert (
        clinic.put(
            f"/v1/marketing/groups/{a['id']}",
            headers=ADMIN,
            json={
                "name": "Renombrado",
                "member_ids": ["ana", "missing"],
                "version": 1,
            },
        ).status_code
        == 422
    )
    assert ok(clinic.get(f"/v1/marketing/groups/{a['id']}", headers=ADMIN))["name"] == "Grupo A"


def test_each_campaign_targets_its_groups_union_deduplicates_and_consent_still_applies(
    clinic: TestClient,
) -> None:
    a = group(clinic, "Grupo A", ["ana", "bea"])
    b = group(clinic, "Grupo B", ["carla", "dora"])
    only_a = campaign(clinic, [a["id"]], "Anuncio A")
    only_b = campaign(clinic, [b["id"]], "Anuncio B")
    union = campaign(clinic, [a["id"], b["id"], a["id"]])
    assert only_a["population"] == {"segment": 2, "eligible": 2, "excluded": {}}
    assert only_b["population"]["eligible"] == 1
    assert union["population"] == {
        "segment": 4,
        "eligible": 3,
        "excluded": {"no_marketing_consent": 1},
    }
    assert union["recipients"] == {"treatment:pending": 3}
    assert [g["name"] for g in only_a["audience_groups"]] == ["Grupo A"]
    assert [g["name"] for g in only_b["audience_groups"]] == ["Grupo B"]
    # Manual selection needs no behavioural/clinical profiling or analytics permission.
    assert not ok(clinic.get("/v1/subjects/ana/consents", headers=ADMIN)).get("analytics")
    ok(
        clinic.put(
            f"/v1/marketing/groups/{a['id']}",
            headers=ADMIN,
            json={
                "name": "A cambiado",
                "member_ids": [],
                "version": 1,
            },
        )
    )
    old = ok(clinic.get(f"/v1/campaigns/{only_a['id']}", headers=ADMIN))
    assert old["audience_groups"] == only_a["audience_groups"]
    assert old["recipients"] == only_a["recipients"]


def test_campaign_cannot_mix_groups_or_target_another_tenant(clinic: TestClient) -> None:
    a = group(clinic, "Grupo A", ["ana"])
    body = {
        "name": "X",
        "kind": "education",
        "segment": "manual_groups",
        "template": GOOD,
        "group_ids": [a["id"]],
        "mode": "simulation",
    }
    assert clinic.post("/v1/campaigns", headers=OTHER, json=body).status_code == 422
    body["group_ids"] = []
    assert clinic.post("/v1/campaigns", headers=ADMIN, json=body).status_code == 422
    body.update(segment="dormant", group_ids=[a["id"]])
    assert clinic.post("/v1/campaigns", headers=ADMIN, json=body).status_code == 422


def test_a_person_must_leave_the_old_group_before_joining_another(clinic: TestClient) -> None:
    a = group(clinic, "Grupo A", ["ana"])
    b = group(clinic, "Grupo B", [])
    path = f"/v1/marketing/groups/{b['id']}"
    move = {"name": "Grupo B", "member_ids": ["ana"], "version": 1}
    assert clinic.put(path, headers=ADMIN, json=move).status_code == 409
    assert ok(clinic.get(path, headers=ADMIN))["member_ids"] == []
    ok(
        clinic.put(
            f"/v1/marketing/groups/{a['id']}",
            headers=ADMIN,
            json={
                "name": "Grupo A",
                "member_ids": [],
                "version": 1,
            },
        )
    )
    assert ok(clinic.put(path, headers=ADMIN, json=move))["member_ids"] == ["ana"]


def test_marketing_role_can_manage_groups_but_other_unrelated_roles_cannot(
    clinic: TestClient,
) -> None:
    a = group(clinic, "Privado", ["ana"])
    for role in ["marketing", "reception", "reviewer", "privacy"]:
        key = ok(
            clinic.post("/v1/admin/staff", headers=ADMIN, json={"name": role, "roles": [role]})
        )["key"]
        headers = {"X-API-Key": key}
        expected = 200 if role == "marketing" else 403
        for path in [
            "/v1/marketing/groups",
            "/v1/marketing/contacts",
            f"/v1/marketing/groups/{a['id']}",
        ]:
            assert clinic.get(path, headers=headers).status_code == expected
        if role == "marketing":
            assert clinic.get("/v1/crm/patients/ana", headers=headers).status_code == 403
    assert clinic.get(f"/v1/marketing/groups/{a['id']}", headers=OTHER).status_code == 404
    assert clinic.get("/v1/marketing/groups", headers=OTHER).json() == []


def test_erasure_removes_memberships_and_restricted_people_are_not_selectable(
    clinic: TestClient,
) -> None:
    a = group(clinic, "Grupo A", ["ana", "bea"])
    exported = ok(clinic.get("/v1/subjects/ana/export", headers=ADMIN))
    assert exported["marketing_groups"] == [{"id": a["id"], "name": "Grupo A"}]
    erased = ok(clinic.delete("/v1/subjects/ana", headers=ADMIN))
    assert erased["marketing_group_memberships"] == 1
    current = ok(clinic.get(f"/v1/marketing/groups/{a['id']}", headers=ADMIN))
    assert current["member_ids"] == ["bea"] and current["version"] == 2
    contacts = ok(clinic.get("/v1/marketing/contacts", headers=ADMIN))["contacts"]
    assert "ana" not in [r["id"] for r in contacts]
    assert (
        clinic.put(
            f"/v1/marketing/groups/{a['id']}",
            headers=ADMIN,
            json={
                "name": "Grupo A",
                "member_ids": ["ana"],
                "version": 2,
            },
        ).status_code
        == 422
    )


def test_consent_is_checked_again_after_the_campaign_is_approved(clinic: TestClient) -> None:
    a = group(clinic, "Grupo A", ["ana"])
    c = campaign(clinic, [a["id"]])
    ok(clinic.post(f"/v1/campaigns/{c['id']}/approve", headers=ADMIN))
    ok(
        clinic.put(
            "/v1/subjects/ana/consents/marketing",
            headers=ADMIN,
            json={"granted": False, "source": "form"},
        )
    )
    ok(clinic.post(f"/v1/campaigns/{c['id']}/send", headers=ADMIN))
    orch = clinic.app.state.orchestrator
    assert clinic.portal is not None
    clinic.portal.call(orch.campaigns.run_outbox)
    result = ok(clinic.get(f"/v1/campaigns/{c['id']}", headers=ADMIN))
    assert result["recipients"] == {"treatment:skipped_no_consent": 1}
