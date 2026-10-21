"""Available, personalized options; all patients and messages are synthetic."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from orchestrator.service import Orchestrator
from tests.test_inbound import ADMIN, SENDER
from tests.test_whatsapp_assistant import app as app


def patient(client: TestClient, pid: str = "p-options", consent: bool = True) -> None:
    assert (
        client.post(
            "/v1/crm/patients",
            headers=ADMIN,
            json={"id": pid, "display_name": "Synthetic Patient", "phone": SENDER},
        ).status_code
        == 201
    )
    if consent:
        assert (
            client.put(
                f"/v1/subjects/{pid}/consents/memory",
                headers=ADMIN,
                json={"granted": True, "source": "synthetic-test"},
            ).status_code
            == 200
        )


def history(
    client: TestClient,
    orch: Orchestrator,
    hour: int,
    status: str = "completed",
    pid: str = "p-options",
    count: int = 4,
) -> None:
    for days in range(1, count + 1):
        start = (datetime.now(orch.agenda.tz) - timedelta(days=days)).replace(
            hour=hour, minute=0, second=0, microsecond=0
        )
        made = client.post(
            "/v1/crm/appointments",
            headers=ADMIN,
            json={
                "patient_id": pid,
                "professional_id": "dra.vera",
                "starts_at": start.isoformat(),
                "duration_min": 50,
            },
        )
        assert made.status_code == 201, made.text
        assert (
            client.post(
                f"/v1/crm/appointments/{made.json()['id']}/status",
                headers=ADMIN,
                json={"status": status},
            ).status_code
            == 200
        )


def compose(orch: Orchestrator, text: str = "Quiero reservar") -> tuple[str | None, bool]:
    return asyncio.run(orch.inbound.assistant.compose("acme", "synthetic-address", SENDER, text))


def options(orch: Orchestrator) -> list[dict[str, Any]]:
    return asyncio.run(orch.inbound.assistant._offer("acme", "synthetic-address")) or []


def test_history_prioritizes_afternoons_and_current_request_overrides_it(
    app: tuple[TestClient, Orchestrator],
) -> None:
    client, orch = app
    patient(client)
    history(client, orch, 17)
    compose(orch)
    assert len(options(orch)) == 3
    assert all(int(o["local"][11:13]) >= 12 for o in options(orch))
    compose(orch, "Esta vez prefiero por la mañana")
    assert all(int(o["local"][11:13]) < 12 for o in options(orch))


def test_explicit_preference_is_recalled_only_with_consent(
    app: tuple[TestClient, Orchestrator],
) -> None:
    client, orch = app
    patient(client)
    compose(orch, "Prefiero las citas por la tarde")
    compose(orch)
    assert all(int(o["local"][11:13]) >= 12 for o in options(orch))
    facts = asyncio.run(orch.memory.export("acme", "p-options"))
    assert any(f.key == "schedule" and f.value == "afternoon" for f in facts)


def test_no_consent_does_not_store_preference(app: tuple[TestClient, Orchestrator]) -> None:
    client, orch = app
    patient(client, consent=False)
    compose(orch, "Prefiero las citas por la tarde")
    assert all(int(o["local"][11:13]) >= 12 for o in options(orch))
    assert asyncio.run(orch.memory.export("acme", "p-options")) == []


def test_model_cannot_replace_the_real_numbered_options(
    app: tuple[TestClient, Orchestrator],
) -> None:
    client, orch = app
    patient(client)
    orch.llm.whatsapp_reply = "Te confirmo una cita: 1) 2099-01-01 03:00"  # type: ignore[attr-defined]
    reply, _ = compose(orch)
    assert reply and "2099-01-01" not in reply
    assert all(f"{i}) {o['local']}" in reply for i, o in enumerate(options(orch), 1))
    assert client.get("/v1/crm/appointments", headers=ADMIN).json() == []


def test_changed_hours_invalidate_offer_before_confirmation(
    app: tuple[TestClient, Orchestrator],
) -> None:
    client, orch = app
    patient(client)
    compose(orch)
    assert options(orch)
    assert (
        client.put(
            "/v1/agenda/professionals/dra.vera/hours", headers=ADMIN, json={"hours": []}
        ).status_code
        == 200
    )
    reply, human = compose(orch, "1")
    assert reply and not reply.startswith("¡Listo")
    assert human
    assert client.get("/v1/crm/appointments", headers=ADMIN).json() == []


def test_occupied_option_refreshes_available_options(app: tuple[TestClient, Orchestrator]) -> None:
    client, orch = app
    patient(client)
    compose(orch)
    chosen = options(orch)[0]
    assert (
        client.post(
            "/v1/crm/appointments",
            headers=ADMIN,
            json={
                "patient_id": "p-options",
                "professional_id": "dra.vera",
                "starts_at": chosen["starts_at"],
                "duration_min": chosen["minutes"],
            },
        ).status_code
        == 201
    )
    reply, _ = compose(orch, "1")
    assert reply and not reply.startswith("¡Listo")
    assert all(o["starts_at"] != chosen["starts_at"] for o in options(orch))
    assert len(client.get("/v1/crm/appointments", headers=ADMIN).json()) == 1


def test_cancelled_visits_are_not_used_as_preferences(
    app: tuple[TestClient, Orchestrator], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, orch = app
    patient(client)
    history(client, orch, 17, status="cancelled")

    async def free(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        return [
            {
                "starts_at": f"2030-01-02T{hour + 5:02}:00:00+00:00",
                "local": f"2030-01-02 {hour:02}:00",
                "minutes": 50,
            }
            for hour in [9, 10, 11, 15, 16, 17]
        ]

    monkeypatch.setattr(orch.agenda, "free_slots", free)
    compose(orch)
    assert all(int(o["local"][11:13]) < 12 for o in options(orch))


def test_fewer_than_three_slots_are_not_invented(
    app: tuple[TestClient, Orchestrator], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, orch = app
    patient(client)

    async def free(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        return [
            {"starts_at": "2030-01-02T14:00:00+00:00", "local": "2030-01-02 09:00", "minutes": 50}
        ]

    monkeypatch.setattr(orch.agenda, "free_slots", free)
    reply, _ = compose(orch)
    assert reply and "1) 2030-01-02 09:00" in reply
    assert "2)" not in reply and "3)" not in reply
    assert "1, 2 o 3" not in reply


def test_schedule_change_between_preflight_and_insert_is_rechecked(
    app: tuple[TestClient, Orchestrator], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, orch = app
    patient(client)
    compose(orch)
    original = orch.agenda.is_free_slot

    async def change_after_check(*args: Any, **kwargs: Any) -> int:
        minutes = await original(*args, **kwargs)
        await orch.agenda.set_hours("acme", "dra.vera", [], "synthetic-test")
        return minutes

    monkeypatch.setattr(orch.agenda, "is_free_slot", change_after_check)
    reply, human = compose(orch, "1")
    assert reply and not reply.startswith("¡Listo") and human
    assert client.get("/v1/crm/appointments", headers=ADMIN).json() == []


def test_a_valid_slot_after_first_200_can_be_confirmed(
    app: tuple[TestClient, Orchestrator],
) -> None:
    client, orch = app
    hours = [
        {"day": d, "hours": "08:00-20:00", "slot_minutes": 10}
        for d in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
    ]
    assert (
        client.put(
            "/v1/agenda/professionals/dra.vera/hours", headers=ADMIN, json={"hours": hours}
        ).status_code
        == 200
    )
    selected = (datetime.now(orch.agenda.tz) + timedelta(days=10)).replace(
        hour=9, minute=0, second=0, microsecond=0
    )
    assert asyncio.run(orch.agenda.is_free_slot("acme", "dra.vera", selected)) == 10


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Quiero una cita mañana", None),
        ("Prefiero por la mañana", "morning"),
        ("Me viene mejor más tarde", "afternoon"),
        ("No puedo por la tarde", None),
        ("¿Por la mañana o por la tarde?", None),
    ],
)
def test_time_preference_does_not_confuse_tomorrow_with_morning(
    text: str, expected: str | None
) -> None:
    from orchestrator.booking_preferences import stated_preference

    assert stated_preference(text) == expected


def test_recent_attendance_can_change_historical_preference() -> None:
    from orchestrator.booking_preferences import historical_preference

    day = datetime(2030, 1, 1)
    assert (
        historical_preference([day.replace(hour=17)] * 4 + [day.replace(hour=9)] * 4) == "afternoon"
    )
    assert (
        historical_preference([day.replace(hour=9)] * 4 + [day.replace(hour=17)] * 4) == "morning"
    )
    assert historical_preference([day.replace(hour=17)]) is None


def test_other_patients_history_does_not_change_options(
    app: tuple[TestClient, Orchestrator], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, orch = app
    patient(client)
    assert (
        client.post(
            "/v1/crm/patients",
            headers=ADMIN,
            json={"id": "other-patient", "display_name": "Other Synthetic"},
        ).status_code
        == 201
    )
    history(client, orch, 17, pid="other-patient")

    async def free(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        return [
            {
                "starts_at": f"2030-01-02T{hour + 5:02}:00:00+00:00",
                "local": f"2030-01-02 {hour:02}:00",
                "minutes": 50,
            }
            for hour in [9, 10, 11, 15, 16, 17]
        ]

    monkeypatch.setattr(orch.agenda, "free_slots", free)
    compose(orch)
    assert all(int(o["local"][11:13]) < 12 for o in options(orch))


def test_revoking_memory_disables_history_and_stored_preferences(
    app: tuple[TestClient, Orchestrator], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, orch = app
    patient(client)
    history(client, orch, 17)
    compose(orch, "Prefiero por la tarde")
    assert (
        client.put(
            "/v1/subjects/p-options/consents/memory",
            headers=ADMIN,
            json={"granted": False, "source": "synthetic-test"},
        ).status_code
        == 200
    )

    async def free(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        return [
            {
                "starts_at": f"2030-01-02T{hour + 5:02}:00:00+00:00",
                "local": f"2030-01-02 {hour:02}:00",
                "minutes": 50,
            }
            for hour in [9, 10, 11, 15, 16, 17]
        ]

    monkeypatch.setattr(orch.agenda, "free_slots", free)
    compose(orch)
    assert all(int(o["local"][11:13]) < 12 for o in options(orch))
