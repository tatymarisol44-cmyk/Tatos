"""The summary before a session (RF-DOC-03): composed by code from the follow-up, the
record's index and the inbox; a draft; never the text of a note; clinicians only, audited.
Synthetic data only."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from orchestrator.api.app import create_app
from orchestrator.briefing import compose, reason_es
from orchestrator.catalog import Catalog
from orchestrator.config import Settings
from orchestrator.followup import no_show_risk
from orchestrator.llm import FakeLLM
from orchestrator.service import Orchestrator

ADMIN = {"X-API-Key": "test-key"}
SECRET_NOTE = "Asociaciones libres sobre la figura paterna; transferencia intensa."
SESSION_NOTE = "Refiere insomnio desde la mudanza; acordamos registro de sueño."


@pytest.fixture
def app(settings: Settings, catalog: Catalog) -> Iterator[tuple[TestClient, Orchestrator]]:
    orch = Orchestrator(settings, catalog=catalog, llm=FakeLLM())
    with TestClient(create_app(settings, orch)) as c:
        yield c, orch


def clinician(client: TestClient, name: str) -> dict[str, str]:
    staff = client.post(
        "/v1/admin/staff", json={"name": name, "roles": ["reviewer"]}, headers=ADMIN
    ).json()
    body = {
        "professional_id": name,
        "display_name": name,
        "pack_id": "ec-psychologist",
        "staff_id": name,
    }
    assert client.post("/v1/admin/professionals", json=body, headers=ADMIN).status_code == 201
    return {"X-API-Key": staff["key"]}


def visit(client: TestClient, days_ago: int, status: str) -> None:
    starts = (datetime.now(UTC) - timedelta(days=days_ago)).isoformat()
    made = client.post(
        "/v1/crm/appointments", json={"patient_id": "p-1", "starts_at": starts}, headers=ADMIN
    ).json()
    if status != "scheduled":
        client.post(
            f"/v1/crm/appointments/{made['id']}/status", json={"status": status}, headers=ADMIN
        )


@pytest.mark.parametrize(
    "args",
    [(1, 3, 3, True, False), (0, 2, 0, True, False), (2, 0, 2, False, False)],
)
def test_every_no_show_reason_reads_in_spanish(args: tuple[int, int, int, bool, bool]) -> None:
    for reason in no_show_risk(*args)["reasons"]:
        assert reason_es(reason) != reason, reason


def test_the_lines_come_from_the_figures_only() -> None:
    facts = {
        "name": "Ana",
        "attendance": {
            "completed": 3,
            "no_shows": 1,
            "cancelled": 0,
            "last_visit": "2026-10-01T15:00:00+00:00",
            "days_since_last_visit": 13,
            "next_visit": None,
        },
        "no_show_risk": {"level": "medium", "reasons": ["no follow-up visit booked"]},
        "tests": [
            {
                "instrument": "GAD-7",
                "applied": 2,
                "first": {"total": 7, "band": "mild"},
                "last": {"total": 16, "band": "severe"},
                "direction": "worse",
                "alerts_last": [],
            }
        ],
    }
    documents = [{"doc_type": "session_note", "created_at": datetime(2026, 10, 1, 15, tzinfo=UTC)}]
    conversations = [{"network": "instagram", "unread": 2, "attention": "human"}]
    lines = compose(
        facts, documents, conversations, ZoneInfo("America/Guayaquil"), datetime.now(UTC)
    )
    text = "\n".join(lines)
    assert "borrador automático" in lines[0]
    assert "No tiene una próxima cita agendada." in text
    assert "01/10/2026 10:00 (hace 13 días)" in text  # Guayaquil time, UTC-5
    assert "Asistencia: 3 de 4 citas (75 %)" in text
    assert "Riesgo de inasistencia medio: no tiene cita de seguimiento agendada." in text
    assert "GAD-7: 16 (severe), antes 7 (mild): empeoró [2 aplicaciones]." in text
    assert "Historia: 1 entradas (1 session_note)" in text
    assert "Mensajes en Instagram: 2 sin leer, pidió hablar con una persona." in text
    assert "no un diagnóstico" in lines[-1]


def test_the_briefing_for_the_professional(app: tuple[TestClient, Orchestrator]) -> None:
    client, _ = app
    client.post("/v1/crm/patients", json={"id": "p-1", "display_name": "Ana"}, headers=ADMIN)
    vera = clinician(client, "dra.vera")
    for days, status in ((60, "completed"), (30, "no_show"), (14, "completed")):
        visit(client, days, status)
    visit(client, -3, "scheduled")
    for doc_type, body in (("session_note", SESSION_NOTE), ("psychotherapy_note", SECRET_NOTE)):
        written = client.post(
            "/v1/clinical/patients/p-1/documents",
            json={"doc_type": doc_type, "body": body},
            headers=vera,
        )
        assert written.status_code == 201, written.text

    response = client.get("/v1/clinical/patients/p-1/briefing", headers=vera)
    assert response.status_code == 200
    briefing = response.json()
    assert briefing["draft"] is True and briefing["prepared_by"] == "system"
    text = briefing["text"]
    assert "Ana" in text and "Próxima cita:" in text and "Asistencia: 2 de 3 citas" in text
    assert "1 psychotherapy_note" in text  # its author sees it counted
    for secret in ("insomnio", "figura paterna"):
        assert secret not in text  # the index, never the text
    assert "briefing.prepared" in client.get("/v1/audit", headers=ADMIN).text

    # Another clinician does not see the psychotherapy note, not even counted.
    other = clinician(client, "dr.luis")
    theirs = client.get("/v1/clinical/patients/p-1/briefing", headers=other).json()
    assert "psychotherapy_note" not in theirs["text"] and "(1 session_note)" in theirs["text"]


def test_clinicians_only_and_known_patients_only(app: tuple[TestClient, Orchestrator]) -> None:
    client, _ = app
    client.post("/v1/crm/patients", json={"id": "p-1", "display_name": "Ana"}, headers=ADMIN)
    reception = client.post(
        "/v1/admin/staff", json={"name": "r", "roles": ["reception"]}, headers=ADMIN
    ).json()
    for headers in (ADMIN, {"X-API-Key": reception["key"]}):
        assert client.get("/v1/clinical/patients/p-1/briefing", headers=headers).status_code == 403
    vera = clinician(client, "dra.vera")
    assert client.get("/v1/clinical/patients/p-404/briefing", headers=vera).status_code == 404
    first = client.get("/v1/clinical/patients/p-1/briefing", headers=vera).json()
    assert "Aún no hay sesiones completadas." in first["text"]
    assert "Historia: sin entradas todavía." in first["text"]
