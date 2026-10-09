"""Register of privacy cases (LOPDP deadlines, docs/legal/README.md): each case gets its
steps and due dates on opening, steps are completed with an outcome, a case closes only
when every step is done, late steps stay visible, the privacy role alone uses it, and the
alert input counts what is due within 24 hours. Synthetic data only."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from orchestrator.api.app import create_app
from orchestrator.catalog import Catalog
from orchestrator.config import Settings
from orchestrator.llm import FakeLLM
from orchestrator.privacy_cases import steps_for
from orchestrator.service import Orchestrator

SERVICE = {"X-API-Key": "test-key"}
GLOBEX = {"X-API-Key": "other-key"}


@pytest.fixture
def world(settings: Settings, catalog: Catalog) -> Iterator[tuple[TestClient, Orchestrator]]:
    orch = Orchestrator(settings, catalog=catalog, llm=FakeLLM())
    with TestClient(create_app(settings, orch)) as c:
        yield c, orch


def person(client: TestClient, name: str, *roles: str) -> dict[str, str]:
    made = client.post(
        "/v1/admin/staff", json={"name": name, "roles": list(roles)}, headers=SERVICE
    )
    assert made.status_code == 201, made.text
    return {"X-API-Key": made.json()["key"]}


def due_days(case: dict[str, Any]) -> dict[str, int]:
    opened = datetime.fromisoformat(case["opened_at"])
    return {s["step"]: (datetime.fromisoformat(s["due_at"]) - opened).days for s in case["steps"]}


def test_the_legal_deadlines_of_each_kind() -> None:
    assert steps_for("access", found_by_platform=False) == [("answer", 15, "LOPDP Art. 13")]
    assert [(s, d) for s, d, _ in steps_for("breach", found_by_platform=True)] == [
        ("notify_controller", 2),
        ("notify_authority", 5),
        ("notify_subjects", 3),
    ]
    assert "notify_controller" not in [
        s for s, _, _ in steps_for("breach", found_by_platform=False)
    ]


def test_a_rights_request_from_arrival_to_closing(
    world: tuple[TestClient, Orchestrator],
) -> None:
    client, _ = world
    dpo = person(client, "dpo", "privacy")
    arrived = (datetime.now(UTC) - timedelta(days=3)).isoformat()
    body = {
        "kind": "access",
        "subject_id": "p-1",
        "summary": "Pidió copia de sus datos por correo.",
        "opened_at": arrived,
    }
    case = client.post("/v1/privacy/cases", json=body, headers=dpo).json()
    assert due_days(case) == {"answer": 15}
    [step] = case["steps"]
    assert step["legal_basis"] == "LOPDP Art. 13" and not step["late"]
    assert 11 * 24 < step["hours_left"] < 12 * 24 + 1

    cid = case["case_id"]
    refused = client.post(f"/v1/privacy/cases/{cid}/close", headers=dpo)
    assert refused.status_code == 422 and "answer" in refused.json()["detail"]
    done = client.post(
        f"/v1/privacy/cases/{cid}/steps/answer",
        json={"outcome": "Exportación enviada el mismo día."},
        headers=dpo,
    )
    assert done.status_code == 200 and done.json()["steps"][0]["done_by"]
    again = client.post(f"/v1/privacy/cases/{cid}/steps/answer", json={"outcome": "x"}, headers=dpo)
    assert again.status_code == 422
    closed = client.post(f"/v1/privacy/cases/{cid}/close", headers=dpo).json()
    assert closed["status"] == "closed"
    assert [
        c["case_id"] for c in client.get("/v1/privacy/cases?status=open", headers=dpo).json()
    ] == []
    audit = client.get("/v1/audit", headers=SERVICE).text
    for action in ("privacy_case.opened", "privacy_case.step_done", "privacy_case.closed"):
        assert action in audit


def test_a_breach_shows_late_steps_and_feeds_the_alert(
    world: tuple[TestClient, Orchestrator],
) -> None:
    client, orch = world
    dpo = person(client, "dpo", "privacy")
    known = (datetime.now(UTC) - timedelta(days=4)).isoformat()
    body = {
        "kind": "breach",
        "summary": "Correo con un listado de citas enviado a un destinatario equivocado.",
        "opened_at": known,
        "found_by_platform": True,
    }
    case = client.post("/v1/privacy/cases", json=body, headers=dpo).json()
    steps = {s["step"]: s for s in case["steps"]}
    assert steps["notify_controller"]["late"] and steps["notify_subjects"]["late"]
    assert not steps["notify_authority"]["late"]  # 5 days: due within 24 hours
    import anyio

    assert anyio.run(orch.privacy_cases.due_soon) == 3
    client.post(
        f"/v1/privacy/cases/{case['case_id']}/steps/notify_subjects",
        json={"outcome": "No requerido: sin riesgo para sus derechos (solo fechas)."},
        headers=dpo,
    )
    assert anyio.run(orch.privacy_cases.due_soon) == 2


def test_only_the_privacy_role_and_only_its_own_tenant(
    world: tuple[TestClient, Orchestrator],
) -> None:
    client, _ = world
    dpo = person(client, "dpo", "privacy")
    for who in (person(client, "recepcion", "reception"), person(client, "dra", "reviewer")):
        assert client.get("/v1/privacy/cases", headers=who).status_code == 403
    body = {"kind": "erasure", "subject_id": "p-1", "summary": "Pidió borrar su contacto."}
    case = client.post("/v1/privacy/cases", json=body, headers=dpo).json()
    assert client.get(f"/v1/privacy/cases/{case['case_id']}", headers=GLOBEX).status_code == 404
    assert client.get("/v1/privacy/cases", headers=GLOBEX).json() == []


@pytest.mark.parametrize(
    ("body", "error"),
    [
        ({"kind": "access", "summary": "x"}, "subject_id"),
        ({"kind": "breach", "summary": "x", "opened_at": "2999-01-01T00:00:00Z"}, "future"),
    ],
)
def test_bad_cases_are_refused(
    world: tuple[TestClient, Orchestrator], body: dict[str, Any], error: str
) -> None:
    client, _ = world
    dpo = person(client, "dpo", "privacy")
    response = client.post("/v1/privacy/cases", json=body, headers=dpo)
    assert response.status_code == 422 and error in response.text
    assert (
        client.post(
            "/v1/privacy/cases", json={"kind": "gossip", "summary": "x"}, headers=dpo
        ).status_code
        == 422
    )
