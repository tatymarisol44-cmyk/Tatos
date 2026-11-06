"""Service feedback is versioned, voluntary, own-patient-only and erased on request."""

from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from orchestrator.db import utcnow
from tests.test_agenda import ADMIN, GLOBEX, patient_key
from tests.test_agenda import client as client

SPEC = {
    "title": "Tu experiencia en el consultorio",
    "questions": [
        {
            "id": "servicio",
            "text": "¿Qué tan satisfecha estás con el servicio?",
            "minimum": 1,
            "maximum": 5,
        },
    ],
    "critical_below": 40,
}


def configure(client: TestClient, channels: list[str] | None = None) -> dict[str, Any]:
    created = client.post("/v1/surveys/definitions", headers=ADMIN, json={"spec": SPEC})
    assert created.status_code == 201, created.text
    definition = created.json()
    result = client.put(
        "/v1/surveys/configuration",
        headers=ADMIN,
        json={
            "revision": 0,
            "settings": {
                "enabled": True,
                "survey_id": definition["id"],
                "version": definition["version"],
                "after_minutes": 0,
                "channels": channels or ["app"],
                "reminder_hours": 1,
                "max_reminders": 1,
            },
        },
    )
    assert result.status_code == 200, result.text
    return definition


def complete(client: TestClient, patient: str = "p-1") -> str:
    visit = client.post(
        "/v1/crm/appointments",
        headers=ADMIN,
        json={
            "patient_id": patient,
            "starts_at": (utcnow() + timedelta(days=1)).isoformat(),
            "duration_min": 50,
        },
    ).json()
    result = client.post(
        f"/v1/crm/appointments/{visit['id']}/status", headers=ADMIN, json={"status": "completed"}
    )
    assert result.status_code == 200, result.text
    return str(visit["id"])


def opt_in(client: TestClient, patient: str = "p-1") -> dict[str, str]:
    key = patient_key(client, patient)
    response = client.put("/v1/me/consents/surveys", headers=key, json={"granted": True})
    assert response.status_code == 200, response.text
    return key


def test_completed_visit_queues_one_own_survey_and_low_response_alert(client: TestClient) -> None:
    configure(client)
    patient = opt_in(client)
    visit = complete(client)
    pending = client.get("/v1/me/surveys", headers=patient).json()
    assert pending["consent"] and len(pending["invitations"]) == 1
    invitation = pending["invitations"][0]
    assert invitation["questions"][0]["maximum"] == 5
    assert (
        client.get("/v1/me/surveys", headers=patient_key(client, "p-2")).json()["invitations"] == []
    )
    url = "/v1/me/surveys/" + invitation["id"]
    assert (
        client.post(
            url, headers=opt_in(client, "p-2"), json={"answers": {"servicio": 1}}
        ).status_code
        == 404
    )
    answer = client.post(url, headers=patient, json={"answers": {"servicio": 1}})
    assert answer.status_code == 200 and "satisfaction" not in answer.json()
    assert client.post(url, headers=patient, json={"answers": {"servicio": 1}}).status_code == 200
    assert client.post(url, headers=patient, json={"answers": {"servicio": 2}}).status_code == 409
    report = client.get("/v1/surveys/report", headers=ADMIN).json()
    assert report["groups"][0]["answered"] == 1 and report["groups"][0]["mean"] == 0
    assert len(report["critical_alerts"]) == 1
    assert (
        client.post(
            f"/v1/surveys/responses/{invitation['id']}/acknowledge", headers=GLOBEX
        ).status_code
        == 404
    )
    assert (
        client.post(
            f"/v1/surveys/responses/{invitation['id']}/acknowledge", headers=ADMIN
        ).status_code
        == 200
    )
    assert client.get("/v1/surveys/report", headers=ADMIN).json()["critical_alerts"] == []
    assert (
        client.post(
            f"/v1/crm/appointments/{visit}/status", headers=ADMIN, json={"status": "completed"}
        ).status_code
        == 409
    )


def test_questions_stay_on_the_assigned_version_and_responses_require_consent(
    client: TestClient,
) -> None:
    definition = configure(client)
    patient = opt_in(client)
    complete(client)
    pending = client.get("/v1/me/surveys", headers=patient).json()["invitations"][0]
    newer = {**SPEC, "questions": [{"id": "otra", "text": "Otra pregunta", "maximum": 10}]}
    revised = client.post(
        "/v1/surveys/definitions",
        headers=ADMIN,
        json={"id": definition["id"], "version": 1, "spec": newer},
    )
    assert revised.status_code == 201 and revised.json()["version"] == 2
    assert (
        client.post(
            "/v1/surveys/definitions",
            headers=ADMIN,
            json={"id": definition["id"], "version": 1, "spec": newer},
        ).status_code
        == 409
    )
    current = client.get("/v1/me/surveys", headers=patient).json()["invitations"][0]
    assert current["questions"][0]["id"] == "servicio"
    url = "/v1/me/surveys/" + pending["id"]
    assert (
        client.post(url, headers=patient, json={"answers": {"servicio": True}}).status_code == 422
    )
    assert client.post(url, headers=patient, json={"answers": {"servicio": 6}}).status_code == 409
    client.put("/v1/me/consents/surveys", headers=patient, json={"granted": False})
    assert client.get("/v1/me/surveys", headers=patient).json()["invitations"] == []
    assert client.post(url, headers=patient, json={"answers": {"servicio": 5}}).status_code == 409


def test_no_consent_means_no_invitation_and_social_channels_never_send(client: TestClient) -> None:
    configure(client, ["app", "whatsapp", "facebook", "instagram"])
    complete(client, "p-2")
    assert client.get("/v1/surveys/report", headers=ADMIN).json()["groups"] == []
    patient = opt_in(client)
    complete(client)
    o = client.app.state.orchestrator
    assert client.portal is not None
    first = client.portal.call(o.surveys.tick, "acme", utcnow() + timedelta(hours=2))
    second = client.portal.call(o.surveys.tick, "acme", utcnow() + timedelta(hours=4))
    assert first == {"app_reminders": 1, "external_blocked": 3, "external_sent": 0}
    assert second["app_reminders"] == 0
    assert (
        client.get("/v1/me/surveys", headers=patient).json()["invitations"][0]["reminder_count"]
        == 1
    )


def test_surveys_export_and_erasure_remove_optional_feedback(client: TestClient) -> None:
    configure(client)
    patient = opt_in(client)
    complete(client)
    invitation = client.get("/v1/me/surveys", headers=patient).json()["invitations"][0]
    client.post(
        "/v1/me/surveys/" + invitation["id"], headers=patient, json={"answers": {"servicio": 4}}
    )
    exported = client.get("/v1/subjects/p-1/export", headers=ADMIN).json()
    assert len(exported["service_surveys"]) == 1
    erased = client.delete("/v1/subjects/p-1", headers=ADMIN)
    assert erased.status_code == 200, erased.text
    assert erased.json()["service_surveys"] == 1
    assert client.get("/v1/surveys/report", headers=ADMIN).json()["groups"] == []


def test_survey_enqueue_failure_rolls_back_appointment_completion(client: TestClient) -> None:
    configure(client)
    opt_in(client)
    o = client.app.state.orchestrator

    async def broken(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("synthetic queue failure")

    o.crm.after_completed = broken
    with pytest.raises(RuntimeError, match="synthetic queue"):
        complete(client)
    appointments = client.get("/v1/crm/appointments", headers=ADMIN).json()
    assert len(appointments) == 1 and appointments[0]["status"] == "scheduled"
    assert client.get("/v1/surveys/report", headers=ADMIN).json()["groups"] == []
