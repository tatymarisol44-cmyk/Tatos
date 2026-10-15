"""The monthly report and SMART goals (RF-MKT-06): a month against the one before, in the
practice's time zone, what stands out, and goals with computed progress. Totals only.
Synthetic data only."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from orchestrator.api.app import create_app
from orchestrator.catalog import Catalog
from orchestrator.config import Settings
from orchestrator.db import utcnow
from orchestrator.llm import FakeLLM
from orchestrator.monthly_report import ReportError, highlights, month_bounds, previous
from orchestrator.service import Orchestrator

ADMIN = {"X-API-Key": "test-key"}
TZ = ZoneInfo("America/Guayaquil")


def test_months_in_the_practices_zone() -> None:
    start, end = month_bounds("2026-12", TZ)
    assert start.isoformat() == "2026-12-01T05:00:00+00:00"  # midnight in Guayaquil
    assert end.isoformat() == "2027-01-01T05:00:00+00:00"
    assert previous("2026-01") == "2025-12" and previous("2026-10") == "2026-09"
    with pytest.raises(ReportError):
        month_bounds("octubre", TZ)


def test_what_stands_out() -> None:
    base = {"nuevos_pacientes": 1, "tasa_asistencia": 90, "mensajes_por_red": {}, "talleres": 1}
    now = {
        "nuevos_pacientes": 3,
        "tasa_asistencia": 70,
        "mensajes_por_red": {"instagram": 9, "whatsapp": 4},
        "talleres": 0,
    }
    said = " ".join(highlights(now, base))
    assert "bajó de 90 % a 70 %" in said and "3 pacientes nuevos" in said
    assert "Instagram (9)" in said and "No hubo talleres" in said


@pytest.fixture
def client(settings: Settings, catalog: Catalog) -> Iterator[TestClient]:
    orch = Orchestrator(settings, catalog=catalog, llm=FakeLLM())
    with TestClient(create_app(settings, orch)) as c:
        yield c


def this_month() -> str:
    now = utcnow().astimezone(TZ)
    return f"{now.year}-{now.month:02d}"


def test_the_report_counts_the_month_and_tracks_goals(client: TestClient) -> None:
    client.post("/v1/crm/patients", json={"id": "p-1", "display_name": "Ana"}, headers=ADMIN)
    month = this_month()
    start, _ = month_bounds(month, TZ)
    # Inside this month whatever the day the test runs.
    for minutes, status in ((10, "completed"), (20, "completed"), (30, "no_show")):
        visit = client.post(
            "/v1/crm/appointments",
            json={
                "patient_id": "p-1",
                "starts_at": (start + timedelta(minutes=minutes)).isoformat(),
            },
            headers=ADMIN,
        ).json()
        client.post(
            f"/v1/crm/appointments/{visit['id']}/status", json={"status": status}, headers=ADMIN
        )
    for metric, target in (("citas_atendidas", 4), ("inasistencias", 2), ("tasa_asistencia", 60)):
        made = client.post(
            "/v1/insights/goals",
            json={"month": month, "metric": metric, "target": target, "note": "Meta sintética"},
            headers=ADMIN,
        )
        assert made.status_code == 201, made.text
    report = client.get(f"/v1/insights/report?month={month}", headers=ADMIN).json()
    metrics = report["metrics"]
    assert (metrics["citas_atendidas"], metrics["inasistencias"], metrics["tasa_asistencia"]) == (
        2,
        1,
        67,
    )
    assert metrics["nuevos_pacientes"] >= 1
    goals = {g["metric"]: g for g in report["goals"]}
    assert goals["citas_atendidas"]["progress"] == 50 and not goals["citas_atendidas"]["met"]
    assert goals["inasistencias"]["met"]  # 1 at or below 2: less is better
    assert goals["tasa_asistencia"]["met"] and goals["tasa_asistencia"]["label"] == "Asistencia (%)"
    assert (
        report["previous"] == previous(month)
        and report["labels"]["talleres"] == "Talleres realizados"
    )
    assert "report.viewed" in client.get("/v1/audit", headers=ADMIN).text


def test_goals_are_checked_and_removed(client: TestClient) -> None:
    month = this_month()
    bad = [
        {"month": month, "metric": "ventas", "target": 5, "note": "x"},
        {"month": "2020-01", "metric": "talleres", "target": 1, "note": "x"},
        {"month": month, "metric": "tasa_asistencia", "target": 120, "note": "x"},
    ]
    for body in bad:
        assert client.post("/v1/insights/goals", json=body, headers=ADMIN).status_code == 422
    made = client.post(
        "/v1/insights/goals",
        json={"month": month, "metric": "talleres", "target": 1, "note": "Comunidad"},
        headers=ADMIN,
    ).json()
    assert client.delete(f"/v1/insights/goals/{made['goal_id']}", headers=ADMIN).status_code == 204
    assert client.delete(f"/v1/insights/goals/{made['goal_id']}", headers=ADMIN).status_code == 404
    marketing = client.post(
        "/v1/admin/staff", json={"name": "m", "roles": ["marketing"]}, headers=ADMIN
    ).json()
    assert (
        client.get("/v1/insights/report", headers={"X-API-Key": marketing["key"]}).status_code
        == 403
    )
