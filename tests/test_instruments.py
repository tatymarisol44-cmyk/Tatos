"""Instruments defined by the professional: any test, scored, versioned and audited.
Synthetic data only; the custom test below is invented for the test."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from orchestrator.api.app import create_app
from orchestrator.catalog import Catalog
from orchestrator.config import Settings
from orchestrator.instruments import TEMPLATES, InstrumentError, InstrumentSpec, score
from orchestrator.llm import FakeLLM
from orchestrator.scales import score as scale_score
from orchestrator.service import Orchestrator

ADMIN = {"X-API-Key": "test-key"}
LICENCE = {"source": "own", "attestation": True}

# An invented 4-item resilience questionnaire with a reversed item, two subscales,
# bands, an alert rule and an open question.
CUSTOM: dict[str, Any] = {
    "name": "Escala de afrontamiento (prueba)",
    "instructions": "Responda pensando en la última semana.",
    "items": [
        {
            "id": "a1",
            "text": "Busco apoyo cuando lo necesito",
            "options": [
                {"label": "Nada", "value": 0},
                {"label": "Algo", "value": 1},
                {"label": "Bastante", "value": 2},
                {"label": "Mucho", "value": 3},
            ],
        },
        {
            "id": "a2",
            "text": "Me cuesta pedir ayuda",
            "reverse": True,
            "options": [
                {"label": "Nada", "value": 0},
                {"label": "Algo", "value": 1},
                {"label": "Bastante", "value": 2},
                {"label": "Mucho", "value": 3},
            ],
        },
        {"id": "a3", "text": "Horas de sueño por noche", "type": "number", "min": 0, "max": 24},
        {"id": "a4", "text": "¿Algo más que quiera contar?", "type": "text", "required": False},
    ],
    "scoring": {
        "method": "sum",
        "subscales": {"apoyo": ["a1", "a2"], "descanso": ["a3"]},
        "bands": [
            {"min": 0, "max": 5, "label": "bajo", "severity": "high"},
            {"min": 5.01, "max": 100, "label": "adecuado", "severity": "none"},
        ],
        "alerts": [{"item": "a3", "op": "lte", "value": 3, "message": "Sueño muy escaso"}],
    },
    "licence": LICENCE,
}


# --- scoring, without the API -------------------------------------------------------------


def test_scoring_reverse_items_subscales_bands_and_alerts() -> None:
    spec = InstrumentSpec.model_validate(CUSTOM)
    result = score(spec, {"a1": 3, "a2": 3, "a3": 2, "a4": "duermo mal"})
    # a2 is reversed: 3 -> 0.  Total = 3 + 0 + 2.
    assert result["total"] == 5
    assert result["subscales"] == {"apoyo": 3, "descanso": 2}
    assert result["band"] == "bajo" and result["severity"] == "high"
    assert result["alerts"] == [{"item": "a3", "message": "Sueño muy escaso"}]
    assert result["answers"]["a4"] == "duermo mal"  # recorded, not scored


@pytest.mark.parametrize(
    ("answers", "error"),
    [
        ({"a1": 3, "a2": 1}, "a3 needs an answer"),
        ({"a1": 7, "a2": 1, "a3": 8}, "not one of its options"),
        ({"a1": 1, "a2": 1, "a3": 30}, "out of range"),
        ({"a1": 1, "a2": 1, "a3": 8, "zz": 1}, "unknown items"),
        ({"a1": "mucho", "a2": 1, "a3": 8}, "expected a number"),
    ],
)
def test_answers_are_validated(answers: dict[str, Any], error: str) -> None:
    with pytest.raises(InstrumentError, match=error):
        score(InstrumentSpec.model_validate(CUSTOM), answers)


@pytest.mark.parametrize(
    ("change", "error"),
    [
        ({"licence": {"source": "x", "attestation": False}}, "right to use"),
        ({"items": [CUSTOM["items"][0], CUSTOM["items"][0]]}, "unique"),
        ({"scoring": {"subscales": {"s": ["nope"]}}}, "unknown items"),
        ({"scoring": {"alerts": [{"item": "nope", "value": 1, "message": "m"}]}}, "unknown"),
    ],
)
def test_definitions_are_validated(change: dict[str, Any], error: str) -> None:
    with pytest.raises(ValidationError, match=error):
        InstrumentSpec.model_validate({**CUSTOM, **change})


@pytest.mark.parametrize("answers", [[0] * 9, [1] * 9, [2] * 9, [3] * 9, [0] * 8 + [1]])
def test_the_phq9_template_scores_like_the_reference_implementation(answers: list[int]) -> None:
    ours = score(TEMPLATES["phq9"], {f"i{n}": v for n, v in enumerate(answers, 1)})
    reference = scale_score("phq9", answers)
    assert ours["total"] == reference.total and ours["band"] == reference.band
    assert bool(ours["alerts"]) == reference.needs_attention == (answers[8] > 0)


# --- the API --------------------------------------------------------------------------------


@pytest.fixture
def client(settings: Settings, catalog: Catalog) -> Iterator[TestClient]:
    settings.tenant_packs = {"acme": "ec-psychologist"}
    orch = Orchestrator(settings, catalog=catalog, llm=FakeLLM())
    with TestClient(create_app(settings, orch)) as c:
        c.post("/v1/crm/patients", json={"id": "p-1", "display_name": "Ana"}, headers=ADMIN)
        yield c


def staff(client: TestClient, name: str, role: str) -> dict[str, str]:
    key = client.post("/v1/admin/staff", json={"name": name, "roles": [role]}, headers=ADMIN)
    return {"X-API-Key": key.json()["key"]}


def test_a_psychologist_brings_their_own_test_and_applies_it(client: TestClient) -> None:
    vera = staff(client, "dra.vera", "reviewer")
    made = client.post("/v1/clinical/instruments", json={"spec": CUSTOM}, headers=vera)
    assert made.status_code == 201, made.text
    iid = made.json()["instrument_id"]
    assert made.json()["version"] == 1
    assert [
        i["instrument_id"] for i in client.get("/v1/clinical/instruments", headers=vera).json()
    ] == [iid]

    answers = {"a1": 2, "a2": 0, "a3": 7}
    applied = client.post(
        "/v1/clinical/patients/p-1/instrument-results",
        json={"instrument_id": iid, "answers": answers},
        headers=vera,
    )
    assert applied.status_code == 201, applied.text
    result = applied.json()
    assert result["total"] == 12 and result["band"] == "adecuado" and result["alerts"] == []

    # Revised later: the old result keeps the version it was scored with.
    revised = {**CUSTOM, "name": "Escala de afrontamiento v2"}
    v2 = client.put(f"/v1/clinical/instruments/{iid}", json={"spec": revised}, headers=vera)
    assert v2.json()["version"] == 2
    [old] = client.get("/v1/clinical/patients/p-1/instrument-results", headers=vera).json()
    assert old["version"] == 1 and old["result_id"] == result["result_id"]
    v1 = client.get(f"/v1/clinical/instruments/{iid}", params={"version": 1}, headers=vera)
    assert v1.json()["name"] == CUSTOM["name"]

    # The audit names the event, never the answers.
    audit = client.get("/v1/audit", headers=ADMIN).text
    assert "instrument.administered" in audit and "instrument.revised" in audit
    assert '"a3"' not in audit

    # The patient's access request includes the result.
    export = client.get("/v1/subjects/p-1/export", headers=ADMIN).json()
    assert export["instrument_results"][0]["total"] == 12


def test_alerts_templates_privacy_and_roles(client: TestClient) -> None:
    vera = staff(client, "dra.vera", "reviewer")
    ruiz = staff(client, "dr.ruiz", "reviewer")
    reception = staff(client, "maria", "reception")

    phq9 = client.post(
        "/v1/clinical/instruments/from-template", json={"template": "phq9"}, headers=vera
    ).json()
    answers = {f"i{n}": 0 for n in range(1, 10)} | {"i9": 1}
    result = client.post(
        "/v1/clinical/patients/p-1/instrument-results",
        json={"instrument_id": phq9["instrument_id"], "answers": answers},
        headers=vera,
    ).json()
    assert result["alerts"] and "autolesión" in result["alerts"][0]["message"]

    private = client.post(
        "/v1/clinical/instruments", json={"spec": CUSTOM, "visibility": "private"}, headers=vera
    ).json()
    assert private["instrument_id"] not in {
        i["instrument_id"] for i in client.get("/v1/clinical/instruments", headers=ruiz).json()
    }
    assert (
        client.get(f"/v1/clinical/instruments/{private['instrument_id']}", headers=ruiz).status_code
        == 404
    )
    # Only the author retires or revises.
    assert (
        client.delete(f"/v1/clinical/instruments/{phq9['instrument_id']}", headers=ruiz).status_code
        == 403
    )
    assert (
        client.delete(f"/v1/clinical/instruments/{phq9['instrument_id']}", headers=vera).status_code
        == 204
    )

    # Reception has no access to tests or results; unknown patients are 404.
    assert client.get("/v1/clinical/instruments", headers=reception).status_code == 403
    assert (
        client.get("/v1/clinical/patients/p-1/instrument-results", headers=reception).status_code
        == 403
    )
    missing = client.post(
        "/v1/clinical/patients/p-404/instrument-results",
        json={"instrument_id": private["instrument_id"], "answers": {}},
        headers=vera,
    )
    assert missing.status_code == 404
    bad = client.post(
        "/v1/clinical/patients/p-1/instrument-results",
        json={"instrument_id": private["instrument_id"], "answers": {"a1": 9}},
        headers=vera,
    )
    assert bad.status_code == 422
