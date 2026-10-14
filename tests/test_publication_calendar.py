"""The publication calendar (RF-PUB-04): schedule a publication, see the month, and the
worker publishes it at its time only if a person approved it. Synthetic data only."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

import orchestrator.publishing as publishing_module
from orchestrator.api.app import create_app
from orchestrator.catalog import Catalog
from orchestrator.config import Settings
from orchestrator.db import utcnow
from orchestrator.llm import FakeLLM
from orchestrator.service import Orchestrator

ADMIN = {"X-API-Key": "test-key"}
GLOBEX = {"X-API-Key": "other-key"}
BRIEF = {
    "title": "Cuidar tu mente también es salud",
    "points": ["Hablarlo ayuda."],
    "cta": "Agenda tu cita en línea",
    "practice_name": "Consultorio Demo",
}


@pytest.fixture
def app(
    settings: Settings, catalog: Catalog, tmp_path: Path
) -> Iterator[tuple[TestClient, Orchestrator]]:
    settings.tenant_packs = {"acme": "ec-psychologist"}
    settings.media_dir = tmp_path / "media"
    orch = Orchestrator(settings, catalog=catalog, llm=FakeLLM())
    with TestClient(create_app(settings, orch)) as c:
        yield c, orch


def publication(client: TestClient) -> str:
    accounts = client.get("/v1/social/accounts", headers=ADMIN).json()
    account = (
        accounts[0]
        if accounts
        else client.post(
            "/v1/social/accounts",
            json={
                "network": "instagram",
                "external_id": "ig-178414",
                "handle": "@consultorio.demo",
                "secret_ref": "IG_DEMO",
            },
            headers=ADMIN,
        ).json()
    )
    made = client.post(
        "/v1/social/publications",
        json={
            "account_id": account["account_id"],
            "kind": "infographic",
            "caption": "Tu bienestar importa.",
            "brief": BRIEF,
        },
        headers=ADMIN,
    )
    assert made.status_code == 201, made.text
    return str(made.json()["publication_id"])


def schedule(client: TestClient, publication_id: str, at: Any, headers: Any = ADMIN) -> Any:
    when = at.isoformat() if at is not None else None
    return client.put(
        f"/v1/social/publications/{publication_id}/schedule", json={"at": when}, headers=headers
    )


def test_schedule_and_see_it_in_the_calendar(app: tuple[TestClient, Orchestrator]) -> None:
    client, _ = app
    publication_id = publication(client)
    at = utcnow() + timedelta(days=3)
    scheduled = schedule(client, publication_id, at)
    assert scheduled.status_code == 200
    assert scheduled.json()["scheduled_for"].startswith(at.isoformat()[:16])
    [entry] = client.get("/v1/social/publications/calendar", headers=ADMIN).json()
    assert entry["publication_id"] == publication_id and entry["overdue"] is False
    assert entry["status"] == "pending_approval"
    # Nothing in another period, and another practice sees nothing.
    later = (utcnow() + timedelta(days=60)).isoformat()
    other = client.get("/v1/social/publications/calendar", params={"from": later}, headers=ADMIN)
    assert other.json() == []
    assert client.get("/v1/social/publications/calendar", headers=GLOBEX).json() == []
    assert schedule(client, publication_id, at, headers=GLOBEX).status_code == 404
    # Off the calendar again.
    assert schedule(client, publication_id, None).json()["scheduled_for"] is None
    assert client.get("/v1/social/publications/calendar", headers=ADMIN).json() == []
    audit = client.get("/v1/audit", headers=ADMIN).text
    assert "publication.scheduled" in audit and "publication.unscheduled" in audit


def test_bad_dates_and_states_are_refused(app: tuple[TestClient, Orchestrator]) -> None:
    client, _ = app
    publication_id = publication(client)
    assert schedule(client, publication_id, utcnow() - timedelta(hours=1)).status_code == 409
    assert schedule(client, publication_id, utcnow() + timedelta(days=400)).status_code == 409
    naive = client.put(
        f"/v1/social/publications/{publication_id}/schedule",
        json={"at": "2030-01-01T10:00:00"},
        headers=ADMIN,
    )
    assert naive.status_code == 422  # a time without its zone is ambiguous
    client.post(f"/v1/social/publications/{publication_id}/cancel", headers=ADMIN)
    assert schedule(client, publication_id, utcnow() + timedelta(days=1)).status_code == 409
    start = utcnow()
    too_long = client.get(
        "/v1/social/publications/calendar",
        params={"from": start.isoformat(), "to": (start + timedelta(days=200)).isoformat()},
        headers=ADMIN,
    )
    assert too_long.status_code == 409


async def test_the_worker_publishes_only_what_was_approved(
    app: tuple[TestClient, Orchestrator], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, orch = app
    waiting, approved = publication(client), publication(client)
    for publication_id in (waiting, approved):
        assert schedule(client, publication_id, utcnow() + timedelta(hours=1)).status_code == 200
    client.post(f"/v1/social/publications/{approved}/approve", headers=ADMIN)
    assert await orch.publications.publish_due() == 0  # not yet time

    later = utcnow() + timedelta(hours=2)
    monkeypatch.setattr(publishing_module, "utcnow", lambda: later)
    assert await orch.publications.publish_due() == 1
    assert await orch.publications.publish_due() == 0  # once only
    done = client.get(f"/v1/social/publications/{approved}", headers=ADMIN).json()
    assert done["status"] == "published" and done["mode"] == "dry_run"  # no credential
    pending = client.get(f"/v1/social/publications/{waiting}", headers=ADMIN).json()
    assert pending["status"] == "pending_approval"  # scheduling never replaces approval

    start = (later - timedelta(days=1)).isoformat()
    calendar = client.get(
        "/v1/social/publications/calendar", params={"from": start}, headers=ADMIN
    ).json()
    overdue = {e["publication_id"]: e["overdue"] for e in calendar}
    assert overdue == {waiting: True, approved: False}
    assert "system:calendar" in client.get("/v1/audit", headers=ADMIN).text
