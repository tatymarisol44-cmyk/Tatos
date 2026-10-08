"""Patient follow-up: attendance, a transparent no-show risk, test trends, and campaign
engagement only with the analytics consent. Synthetic data only."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from orchestrator.api.app import create_app
from orchestrator.catalog import Catalog
from orchestrator.config import Settings
from orchestrator.followup import no_show_risk
from orchestrator.llm import FakeLLM
from orchestrator.service import Orchestrator

ADMIN = {"X-API-Key": "test-key"}


@pytest.mark.parametrize(
    ("args", "level"),
    [
        ((5, 0, 0, False, True), "low"),
        ((5, 0, 0, False, False), "medium"),  # no follow-up booked
        ((3, 2, 0, True, False), "high"),  # misses + the last one missed + nothing booked
        ((1, 1, 0, False, True), "high"),  # half the visits missed
    ],
)
def test_no_show_risk_is_a_transparent_rule(
    args: tuple[int, int, int, bool, bool], level: str
) -> None:
    risk = no_show_risk(*args)
    assert risk["level"] == level
    assert (risk["reasons"] == []) == (level == "low")


@pytest.fixture
def client(settings: Settings, catalog: Catalog) -> Iterator[TestClient]:
    orch = Orchestrator(settings, catalog=catalog, llm=FakeLLM())
    with TestClient(create_app(settings, orch)) as c:
        yield c


def visit(c: TestClient, pid: str, days_ago: int, status: str) -> None:
    starts = (datetime.now(UTC) - timedelta(days=days_ago)).isoformat()
    made = c.post(
        "/v1/crm/appointments", json={"patient_id": pid, "starts_at": starts}, headers=ADMIN
    ).json()
    if status != "scheduled":
        c.post(f"/v1/crm/appointments/{made['id']}/status", json={"status": status}, headers=ADMIN)


def test_the_follow_up_of_a_patient_and_the_worklist(client: TestClient) -> None:
    staff = client.post(
        "/v1/admin/staff", json={"name": "dra", "roles": ["reviewer"]}, headers=ADMIN
    ).json()
    vera = {"X-API-Key": staff["key"]}
    client.post("/v1/crm/patients", json={"id": "p-1", "display_name": "Ana"}, headers=ADMIN)
    client.post("/v1/crm/patients", json={"id": "p-2", "display_name": "Luis"}, headers=ADMIN)
    for days, status in ((90, "completed"), (60, "no_show"), (45, "completed"), (40, "no_show")):
        visit(client, "p-1", days, status)
    visit(client, "p-2", 10, "completed")
    visit(client, "p-2", -5, "scheduled")  # booked in five days

    gad7 = client.post(
        "/v1/clinical/instruments/from-template", json={"template": "gad7"}, headers=vera
    ).json()
    for score in (1, 3):  # mild, then severe: worse
        client.post(
            "/v1/clinical/patients/p-1/instrument-results",
            json={
                "instrument_id": gad7["instrument_id"],
                "answers": {f"i{n}": score for n in range(1, 8)},
            },
            headers=vera,
        )

    ana = client.get("/v1/clinical/patients/p-1/follow-up", headers=vera).json()
    assert ana["attendance"]["completed"] == 2 and ana["attendance"]["no_shows"] == 2
    assert ana["attendance"]["attendance_rate"] == 0.5 and ana["attendance"]["next_visit"] is None
    assert ana["no_show_risk"]["level"] == "high" and ana["no_show_risk"]["reasons"]
    [t] = ana["tests"]
    assert (
        t["direction"] == "worse" and t["first"]["band"] == "mild" and t["last"]["band"] == "severe"
    )
    assert ana["engagement"] == {"available": False, "reason": "no analytics consent"}
    assert "no-show risk" in ana["flags"] and "a test got worse" in ana["flags"]

    luis = client.get("/v1/clinical/patients/p-2/follow-up", headers=vera).json()
    assert luis["flags"] == [] and luis["no_show_risk"]["level"] == "low"

    worklist = client.get("/v1/clinical/follow-up", headers=vera).json()
    assert [w["patient_id"] for w in worklist] == ["p-1"]
    # Clinicians only; reception sees agendas, not clinical follow-up.
    reception = client.post(
        "/v1/admin/staff", json={"name": "r", "roles": ["reception"]}, headers=ADMIN
    ).json()
    assert (
        client.get("/v1/clinical/follow-up", headers={"X-API-Key": reception["key"]}).status_code
        == 403
    )
    assert client.get("/v1/clinical/patients/p-404/follow-up", headers=vera).status_code == 404


def test_engagement_only_with_the_analytics_consent(client: TestClient) -> None:
    staff = client.post(
        "/v1/admin/staff", json={"name": "dra", "roles": ["reviewer"]}, headers=ADMIN
    ).json()
    vera = {"X-API-Key": staff["key"]}
    client.post("/v1/crm/patients", json={"id": "p-3", "display_name": "Sofía"}, headers=ADMIN)
    client.put(
        "/v1/subjects/p-3/consents/analytics",
        json={"granted": True, "source": "app"},
        headers=ADMIN,
    )
    view = client.get("/v1/clinical/patients/p-3/follow-up", headers=vera).json()
    assert view["engagement"]["available"] is True and view["engagement"]["campaign_messages"] == 0
