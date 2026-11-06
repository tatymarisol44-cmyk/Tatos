"""Fictional bands only: deterministic scoring of professionally verified image answers."""

import copy
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from orchestrator.instruments import InstrumentError, InstrumentSpec, score
from orchestrator.test_scoring import PixelAnswer, proposals, snapshot
from orchestrator.test_vision import VisualExtraction, descriptive_only
from tests.test_test_images import client as client
from tests.test_test_images import staff, upload

SPEC = {
    "name": "Baremo ficticio de software",
    "items": [
        {
            "id": f"i{n}",
            "text": f"Pregunta ficticia {n}",
            "reverse": n == 2,
            "options": [{"label": str(value), "value": value} for value in range(4)],
        }
        for n in range(1, 4)
    ],
    "scoring": {
        "method": "sum",
        "bands": [
            {"min": 0, "max": 2, "label": "Grupo ficticio A"},
            {"min": 3, "max": 5, "label": "Grupo ficticio B"},
            {"min": 6, "max": 9, "label": "Grupo ficticio C"},
        ],
    },
    "licence": {"source": "Propio, solo prueba de software", "attestation": True},
}
ANSWERS = {"i1": 2, "i2": 1, "i3": 0}


def make_instrument(client: TestClient, headers: dict[str, str]) -> dict[str, Any]:
    response = client.post("/v1/clinical/instruments", json={"spec": SPEC}, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


def bind(
    client: TestClient, headers: dict[str, str], kind: str = "questionnaire"
) -> tuple[str, dict[str, Any]]:
    definition = make_instrument(client, headers)
    report = upload(client, headers, kind).json()
    path = f"/v1/clinical/test-images/{report['report_id']}"
    response = client.patch(
        path,
        json={"revision": 1, "instrument_id": definition["instrument_id"], "instrument_version": 1},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    return path, response.json()


def test_verified_answers_produce_band_and_preserve_pinned_version(client: TestClient) -> None:
    author = staff(client)
    path, report = bind(client, author)
    assert report["score_data"]["result"] is None
    for answers in ({"i1": 2}, {**ANSWERS, "i1": True}, {**ANSWERS, "i1": 8}):
        response = client.post(
            path + "/score",
            json={"revision": 2, "answers": answers, "confirmed": True},
            headers=author,
        )
        assert response.status_code == 422, response.text
    assert (
        client.post(
            path + "/score",
            json={"revision": 2, "answers": ANSWERS, "confirmed": False},
            headers=author,
        ).status_code
        == 422
    )
    # Changing the live definition must not change an already associated image.
    changed = copy.deepcopy(SPEC)
    changed["scoring"]["bands"][1]["label"] = "Otra banda ficticia"
    revised = client.put(
        f"/v1/clinical/instruments/{report['score_data']['instrument_id']}",
        json={"spec": changed, "expected_version": 1},
        headers=author,
    )
    assert revised.status_code == 200
    scored = client.post(
        path + "/score", json={"revision": 2, "answers": ANSWERS, "confirmed": True}, headers=author
    )
    assert scored.status_code == 200, scored.text
    data = scored.json()["score_data"]
    assert data["result"]["total"] == 4 and data["result"]["band"] == "Grupo ficticio B"
    assert data["version"] == 1 and data["verified"]
    assert "versión de configuración 1" in scored.json()["scoring_summary"]
    final = client.patch(
        path,
        json={"revision": 3, "body": "Informe ficticio revisado.", "reviewed": True},
        headers=author,
    )
    assert final.status_code == 200
    assert (
        client.post(
            path + "/score",
            json={"revision": 4, "answers": ANSWERS, "confirmed": True},
            headers=author,
        ).status_code
        == 409
    )


def test_context_changes_require_rechecking_and_foreign_author_cannot_score(
    client: TestClient,
) -> None:
    author, colleague = staff(client), staff(client)
    path, _ = bind(client, author)
    assert (
        client.post(
            path + "/score",
            json={"revision": 2, "answers": ANSWERS, "confirmed": True},
            headers=colleague,
        ).status_code
        == 404
    )
    assert (
        client.post(
            path + "/score",
            json={"revision": 2, "answers": ANSWERS, "confirmed": True},
            headers=author,
        ).status_code
        == 200
    )
    changed = client.patch(
        path, json={"revision": 3, "application": {"age_years": 40}}, headers=author
    )
    assert changed.status_code == 200 and changed.json()["score_data"]["result"] is None
    assert (
        client.patch(
            path, json={"revision": 4, "body": "No revisado.", "reviewed": True}, headers=author
        ).status_code
        == 422
    )


def test_a_drawing_cannot_be_associated_to_automatic_questionnaire_bands(
    client: TestClient,
) -> None:
    author = staff(client)
    definition = make_instrument(client, author)
    report = upload(client, author, "drawing").json()
    response = client.patch(
        f"/v1/clinical/test-images/{report['report_id']}",
        json={"revision": 1, "instrument_id": definition["instrument_id"], "instrument_version": 1},
        headers=author,
    )
    assert response.status_code == 422


def test_ambiguous_duplicate_or_invalid_pixel_readings_do_not_become_answers() -> None:
    data = snapshot(
        {
            "instrument_id": "fictional",
            "version": 1,
            "name": SPEC["name"],
            "status": "active",
            "spec": SPEC,
        },
        "questionnaire",
    )
    result = proposals(
        data,
        [
            PixelAnswer(item_id="i1", value=2, quality="uncertain"),
            PixelAnswer(item_id="i2", value=1, quality="clear"),
            PixelAnswer(item_id="i2", value=3, quality="clear"),
            PixelAnswer(item_id="i3", value=8, quality="clear"),
        ],
    )
    assert all(item["value"] is None for item in result["proposals"])
    assert result["result"] is None and not result["verified"]
    with pytest.raises(ValidationError):
        PixelAnswer(item_id="i1", value=True)
    with pytest.raises(ValidationError):
        PixelAnswer(item_id="i1", region=(0.5, 0, 0.2, 1))


def test_overlapping_bands_and_nonfinite_answers_are_rejected() -> None:
    overlapping = copy.deepcopy(SPEC)
    overlapping["scoring"]["bands"][0]["max"] = 3
    with pytest.raises(ValidationError, match="overlap"):
        InstrumentSpec.model_validate(overlapping)
    spec = InstrumentSpec.model_validate(SPEC)
    with pytest.raises(InstrumentError, match="finite"):
        score(spec, {**ANSWERS, "i1": float("nan")})


def test_stale_definition_edit_is_refused(client: TestClient) -> None:
    author = staff(client)
    definition = make_instrument(client, author)
    path = f"/v1/clinical/instruments/{definition['instrument_id']}"
    body = {"spec": SPEC, "expected_version": 1}
    assert client.put(path, json=body, headers=author).status_code == 200
    assert client.put(path, json=body, headers=author).status_code == 409


def test_visual_report_excludes_negative_clinical_inferences_but_keeps_quoted_text() -> None:
    extraction = VisualExtraction(
        visible_text="PHQ-9 (depresión), texto de ejemplo.",
        observations=["Tres filas de texto.", "El dibujo indica ansiedad."],
        uncertainties=["No hay trazos que indiquen abuso, trauma o enfermedad."],
        answers=[
            PixelAnswer(
                item_id="i1", value=2, quality="clear", evidence="La personalidad indica 2."
            )
        ],
    )
    result = descriptive_only(extraction)
    assert result.visible_text == extraction.visible_text
    assert result.observations == ["Tres filas de texto."]
    assert not any("trauma" in line for line in result.uncertainties)
    assert result.answers[0].value is None and result.answers[0].quality == "uncertain"
