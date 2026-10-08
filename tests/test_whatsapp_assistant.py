"""The WhatsApp assistant: natural answers from the model, never clinical advice, and
bookings done by code from numbered options. Synthetic numbers (555) only."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from orchestrator.api.app import create_app
from orchestrator.catalog import Catalog
from orchestrator.config import Settings
from orchestrator.llm import FakeLLM
from orchestrator.service import Orchestrator
from orchestrator.whatsapp_assistant import unsafe_reason
from tests.test_inbound import ADMIN, SECRET, SENDER, connect, payload, post, sent_texts

EVERY_DAY = [
    {"day": d, "hours": "08:00-20:00", "slot_minutes": 50}
    for d in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
]


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("Te recomiendo tomar paracetamol para dormir.", "clinical"),
        ("Deberías practicar respiración profunda cada noche.", "therapeutic advice"),
        ("Lo que sientes es un trastorno de ansiedad.", "therapeutic advice"),
        ("Prueba esta técnica de relajación muscular.", "therapeutic advice"),
        ("Agenda aquí: https://example.test/agenda", "link"),
        ("x" * 800, "too long"),
        ("   ", "empty"),
    ],
)
def test_unsafe_answers_are_caught(text: str, reason: str) -> None:
    assert unsafe_reason(text) == reason


@pytest.mark.parametrize(
    "text",
    [
        "¡Hola! Atendemos de lunes a viernes de 8:00 a 20:00 😊",
        "Eso lo conversarás con tu psicóloga en la sesión. ¿Te ayudo a agendar?",
        "La primera consulta cuesta desde 35 USD.",
    ],
)
def test_everyday_answers_pass(text: str) -> None:
    assert unsafe_reason(text) is None


@pytest.fixture
def app(settings: Settings, catalog: Catalog) -> Iterator[tuple[TestClient, Orchestrator]]:
    settings.whatsapp_verify_token = SecretStr("verify")
    settings.meta_app_secret = SecretStr(SECRET)
    orch = Orchestrator(settings, catalog=catalog, llm=FakeLLM())
    with TestClient(create_app(settings, orch)) as c:
        c.post(
            "/v1/admin/professionals",
            json={
                "professional_id": "dra.vera",
                "display_name": "Dra Vera",
                "pack_id": "ec-psychologist",
            },
            headers=ADMIN,
        )
        c.put("/v1/agenda/professionals/dra.vera/hours", json={"hours": EVERY_DAY}, headers=ADMIN)
        connect(c)
        yield c, orch


def appointments_of(client: TestClient, pid: str) -> list[dict[str, Any]]:
    rows = client.get("/v1/crm/appointments", headers=ADMIN).json()
    return [a for a in rows if a["patient_id"] == pid]


def test_a_known_patient_books_by_answering_a_number(
    app: tuple[TestClient, Orchestrator], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, orch = app
    # The registered phone is written differently from WhatsApp's: the last 9 digits match.
    client.post(
        "/v1/crm/patients",
        json={"id": "p-7", "display_name": "Camila Paredes", "phone": "+1 (555) 000-1111"},
        headers=ADMIN,
    )
    seen = sent_texts(orch, monkeypatch)
    assert post(client, payload("Hola, quisiera una cita esta semana", "wamid.a1")) == 200
    offer = seen[-1]["text"]["body"]
    assert "Gracias por escribirnos" in offer and "1) " in offer and "3) " in offer
    # What the model saw: real slots and the first name; no clinical record exists for it.
    [call] = [c for c in orch.llm.calls if "WHATSAPP_ASSISTANT" in c[0]["content"]]  # type: ignore[attr-defined]
    assert "Camila" in call[1]["content"] and "<horarios>" in call[1]["content"]

    assert post(client, payload("2", "wamid.a2")) == 200
    confirmation = seen[-1]["text"]["body"]
    assert confirmation.startswith("¡Listo, Camila!")
    [booked] = appointments_of(client, "p-7")
    assert booked["professional_id"] == "dra.vera" and booked["status"] == "scheduled"
    second_option = next(line for line in offer.splitlines() if line.startswith("2) "))
    assert second_option[3:] in confirmation  # exactly the time it was offered
    # The message texts are not in the audit, nor stored anywhere.
    audit = client.get("/v1/audit", headers=ADMIN).text
    assert "quisiera una cita" not in audit and "channel.auto_reply_sent" in audit


def test_an_unknown_number_choosing_a_time_reaches_a_person(
    app: tuple[TestClient, Orchestrator], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, orch = app
    seen = sent_texts(orch, monkeypatch)
    post(client, payload("hola, ¿tienen turnos?", "wamid.u1"))
    post(client, payload("1", "wamid.u2"))
    assert "una persona del equipo te escribirá" in seen[-1]["text"]["body"]
    alerts = client.get("/v1/social/alerts", headers=ADMIN).json()
    assert [a["kind"] for a in alerts] == ["human"]
    assert client.get("/v1/crm/appointments", headers=ADMIN).json() == []


def test_an_unsafe_or_failed_model_answer_falls_back_to_the_fixed_text(
    app: tuple[TestClient, Orchestrator], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, orch = app
    seen = sent_texts(orch, monkeypatch)
    orch.llm.whatsapp_reply = "Deberías practicar respiración profunda antes de dormir."  # type: ignore[attr-defined]
    post(client, payload("no puedo dormir, ¿qué hago?", "wamid.f1"))
    body = seen[-1]["text"]["body"]
    assert "respiración" not in body and "Gracias por escribir a" in body  # fixed welcome

    async def down(*args: Any, **kwargs: Any) -> Any:
        raise httpx.ConnectError("provider down")

    monkeypatch.setattr(orch.llm, "complete", down)
    later = payload("hola otra vez", "wamid.f2")
    post(client, later)  # within 12 h of the fixed welcome: nothing more is sent
    assert len(seen) == 1


def test_a_crisis_never_reaches_the_model(
    app: tuple[TestClient, Orchestrator], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, orch = app
    seen = sent_texts(orch, monkeypatch)
    before = len(orch.llm.calls)  # type: ignore[attr-defined]
    post(client, payload("ya no quiero vivir", "wamid.c1"))
    assert "ECU 911" in seen[-1]["text"]["body"]
    assert len(orch.llm.calls) == before  # type: ignore[attr-defined]
    assert SENDER not in client.get("/v1/audit", headers=ADMIN).text
