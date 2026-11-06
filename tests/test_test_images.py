"""Synthetic images and mocked private inference: never clinical/model validation."""

from __future__ import annotations

import base64
import importlib.util
import io
import json
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw

from orchestrator.api.app import create_app
from orchestrator.catalog import Catalog
from orchestrator.config import Settings
from orchestrator.llm import FakeLLM
from orchestrator.service import Orchestrator
from orchestrator.test_vision import PrivateVision, VisualExtraction, prepare_image, private_origin

ADMIN = {"X-API-Key": "test-key"}
OTHER = {"X-API-Key": "other-key"}
PARAMETERS = {
    "version": "Edición ficticia 2026",
    "criteria": "Verificar texto y marcas visibles.",
    "scoring_rules": "No aplicar puntuación en esta prueba ficticia.",
    "source": "Criterios propios para pruebas de software.",
    "reference_population": "Datos sintéticos, sin baremo clínico.",
}
APPLICATION = {
    "applied_on": "2026-11-03",
    "age_years": 30,
    "age_months": 2,
    "context": "Aplicación ficticia.",
}


def image_bytes() -> bytes:
    image = Image.new("RGB", (300, 160), "white")
    ImageDraw.Draw(image).text((12, 12), "TEST SINTETICO\nRespuesta A: 2", fill="black")
    result = io.BytesIO()
    image.save(result, format="PNG")
    return result.getvalue()


@pytest.fixture
def client(settings: Settings, catalog: Catalog) -> Iterator[TestClient]:
    settings.clinical_file_max_bytes = 4096
    orch = Orchestrator(settings, catalog=catalog, llm=FakeLLM())
    with TestClient(create_app(settings, orch)) as c:
        c.post(
            "/v1/crm/patients",
            json={"id": "p-image", "display_name": "Caso ficticio"},
            headers=ADMIN,
        )
        yield c


def staff(client: TestClient, role: str = "reviewer", tenant: str = "acme") -> dict[str, str]:
    made = client.post(
        "/v1/admin/staff",
        json={"name": "persona." + uuid.uuid4().hex, "roles": [role]},
        headers=ADMIN if tenant == "acme" else OTHER,
    )
    assert made.status_code == 201
    return {"X-API-Key": made.json()["key"]}


def upload(
    client: TestClient, headers: dict[str, str], kind: str = "questionnaire", **kwargs: Any
) -> httpx.Response:
    return client.post(
        "/v1/clinical/patients/p-image/test-images",
        headers=headers,
        data={
            "title": "Prueba sintética",
            "kind": kind,
            "parameters": json.dumps(kwargs.get("parameters", PARAMETERS)),
            "application": json.dumps(kwargs.get("application", APPLICATION)),
        },
        files={"file": ("test.png", kwargs.get("content", image_bytes()), "image/png")},
    )


def test_private_upload_manual_review_conflict_export_and_immutability(client: TestClient) -> None:
    author = staff(client)
    response = upload(client, author)
    assert response.status_code == 201, response.text
    report = response.json()
    url = f"/v1/clinical/test-images/{report['report_id']}"
    assert report["status"] == "uploaded" and not report["body"]
    assert (
        client.get(f"/v1/clinical/files/{report['file_id']}", headers=author).content
        == image_bytes()
    )
    unavailable = client.post(
        url + "/analyze", json={"revision": 1, "confirmed": True}, headers=author
    )
    assert unavailable.status_code == 503
    assert client.get(url, headers=author).json()["revision"] == 1
    draft = client.patch(
        url, json={"revision": 1, "body": "Descripción escrita por la profesional."}, headers=author
    )
    assert draft.status_code == 200 and draft.json()["status"] == "draft"
    assert (
        client.patch(
            url, json={"revision": 1, "body": "Cambio obsoleto"}, headers=author
        ).status_code
        == 409
    )
    final = client.patch(
        url, json={"revision": 2, "body": "Informe comprobado.", "reviewed": True}, headers=author
    )
    assert final.status_code == 200 and final.json()["reviewed_at"]
    assert client.delete(url + "?revision=3", headers=author).status_code == 409
    assert (
        client.post(
            url + "/analyze", json={"revision": 3, "confirmed": True}, headers=author
        ).status_code
        == 409
    )
    exported = client.get("/v1/subjects/p-image/export", headers=ADMIN).json()
    assert exported["test_image_reports"] == {"reports": [], "restricted_reports_withheld": 1}
    assert "Informe comprobado" not in json.dumps(exported)
    audit = client.get("/v1/audit", headers=ADMIN).text
    assert "test_image.reviewed" in audit and "Descripción escrita" not in audit


def test_author_role_and_tenant_isolation_and_draft_deletion(client: TestClient) -> None:
    author, colleague, intruder = staff(client), staff(client), staff(client, tenant="globex")
    report = upload(client, author, "drawing").json()
    url = f"/v1/clinical/test-images/{report['report_id']}"
    for denied in (colleague, intruder):
        assert client.get(url, headers=denied).status_code == 404
        assert (
            client.get(f"/v1/clinical/files/{report['file_id']}", headers=denied).status_code == 404
        )
        assert (
            client.patch(url, json={"revision": 1, "body": "intruso"}, headers=denied).status_code
            == 404
        )
        assert client.delete(url, headers=denied).status_code == 404
        assert (
            client.post(
                url + "/analyze", json={"revision": 1, "confirmed": True}, headers=denied
            ).status_code
            == 404
        )
    for role in ("reception", "admin", "owner", "marketing", "privacy"):
        denied = staff(client, role)
        assert upload(client, denied).status_code == 403
        assert client.get(url, headers=denied).status_code == 403
    assert client.get("/v1/clinical/patients/p-image/test-images", headers=colleague).json() == []
    assert client.delete(url + "?revision=1", headers=author).status_code == 204
    assert client.get(f"/v1/clinical/files/{report['file_id']}", headers=author).status_code == 404


@pytest.mark.parametrize(
    "content", [b"", b"%PDF-1.7", b"<svg/>", b"\x89PNG\r\n\x1a\ninvalid", b"x" * 5000]
)
def test_invalid_or_oversized_content_is_never_stored(client: TestClient, content: bytes) -> None:
    author = staff(client)
    assert upload(client, author, content=content).status_code in (413, 422)
    assert client.get("/v1/clinical/patients/p-image/test-images", headers=author).json() == []
    assert client.get("/v1/clinical/patients/p-image/files", headers=author).json() == []


def test_erasure_restriction_blocks_new_analysis_and_changes(client: TestClient) -> None:
    author = staff(client)
    report = upload(client, author).json()
    url = f"/v1/clinical/test-images/{report['report_id']}"
    erased = client.delete("/v1/subjects/p-image", headers=ADMIN)
    assert erased.status_code == 200, erased.text
    assert upload(client, author).status_code == 403
    assert (
        client.post(
            url + "/analyze", json={"revision": 1, "confirmed": True}, headers=author
        ).status_code
        == 403
    )
    assert (
        client.patch(url, json={"revision": 1, "body": "No permitido"}, headers=author).status_code
        == 403
    )
    assert client.get(url, headers=author).status_code == 200


@pytest.mark.parametrize("kind", ["questionnaire", "drawing"])
def test_private_vision_contract_and_draft_never_auto_reviews(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    orch = client.app.state.orchestrator  # type: ignore[attr-defined]
    orch.settings.clinical_vision_enabled = True
    orch.settings.clinical_vision_model = "vision-synthetic"
    calls: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "127.0.0.1"
        calls.append(request.url.path)
        payload = json.loads(request.content)
        if request.url.path == "/api/show":
            return httpx.Response(200, json={"capabilities": ["vision", "completion"]})
        assert payload["stream"] is False and payload["format"]["type"] == "object"
        message = payload["messages"][-1]
        assert "p-image" not in message["content"]
        decoded = base64.b64decode(message["images"][0])
        assert Image.open(io.BytesIO(decoded)).format == "JPEG"
        result = {
            "visible_text": "TEST SINTETICO",
            "observations": ["Marca en A: 2"],
            "uncertainties": ["Verificar la marca."],
        }
        return httpx.Response(200, json={"done": True, "message": {"content": json.dumps(result)}})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: real_client(**kwargs, transport=httpx.MockTransport(respond)),
    )
    author = staff(client)
    report = upload(client, author, kind).json()
    url = f"/v1/clinical/test-images/{report['report_id']}/analyze"
    assert (
        client.post(url, json={"revision": 1, "confirmed": False}, headers=author).status_code
        == 422
    )
    assert calls == []
    response = client.post(url, json={"revision": 1, "confirmed": True}, headers=author)
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["status"] == "draft" and data["reviewed_at"] is None
    assert "TEST SINTETICO" in data["body"] and "Valoración de la profesional" in data["body"]
    assert calls == ["/api/show", "/api/chat"]
    assert not orch.llm.calls


@pytest.mark.parametrize(
    "value",
    [
        "https://api.example.com",
        "http://169.254.169.254",
        "http://127.0.0.1/api",
        "http://user:pass@localhost:11434",
        "http://0.0.0.0",
    ],
)
def test_public_or_metadata_destinations_are_rejected(value: str) -> None:
    with pytest.raises(ValueError):
        private_origin(value)


def test_decode_strips_metadata_and_preserves_visible_image() -> None:
    cleaned = prepare_image(image_bytes())
    with Image.open(io.BytesIO(cleaned)) as image:
        assert image.size == (300, 160) and not image.getexif()


def test_transparent_png_is_read_against_a_white_page() -> None:
    original = Image.new("RGBA", (100, 100), (0, 0, 0, 0))
    output = io.BytesIO()
    original.save(output, format="PNG")
    with Image.open(io.BytesIO(prepare_image(output.getvalue()))) as cleaned:
        assert cleaned.getpixel((0, 0)) == (255, 255, 255)


@pytest.mark.parametrize("mode", ["remote", "no-vision", "malformed", "incomplete", "outage"])
def test_provider_failures_preserve_the_source_and_never_review(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    orch = client.app.state.orchestrator  # type: ignore[attr-defined]
    orch.settings.clinical_vision_enabled = True
    orch.settings.clinical_vision_model = "synthetic-local-model"
    calls: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/api/show":
            if mode == "remote":
                return httpx.Response(
                    200, json={"capabilities": ["vision"], "remote_host": "https://example.invalid"}
                )
            return httpx.Response(
                200, json={"capabilities": [] if mode == "no-vision" else ["vision"]}
            )
        if mode == "outage":
            return httpx.Response(500)
        return httpx.Response(
            200, json={"done": mode != "incomplete", "message": {"content": "invalid-json"}}
        )

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: real_client(**kwargs, transport=httpx.MockTransport(respond)),
    )
    author = staff(client)
    report = upload(client, author).json()
    url = f"/v1/clinical/test-images/{report['report_id']}"
    response = client.post(
        url + "/analyze", json={"revision": 1, "confirmed": True}, headers=author
    )
    assert response.status_code == 503, response.text
    if mode in ("remote", "no-vision"):
        assert calls == ["/api/show"]  # never send the image to an ineligible model
    assert client.get(url, headers=author).json()["status"] == "uploaded"
    assert client.get(f"/v1/clinical/files/{report['file_id']}", headers=author).status_code == 200


def test_a_manual_change_during_inference_is_not_overwritten(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    orch = client.app.state.orchestrator  # type: ignore[attr-defined]
    author = staff(client)
    report = upload(client, author).json()
    url = f"/v1/clinical/test-images/{report['report_id']}"

    async def edit_during_inference(
        self: PrivateVision, content: bytes, kind: str
    ) -> VisualExtraction:
        from sqlalchemy import update

        from orchestrator.test_images import test_image_reports

        async with orch.db.engine.begin() as conn:
            await conn.execute(
                update(test_image_reports).values(
                    revision=2, body="Cambio concurrente.", status="draft"
                )
            )
        return VisualExtraction(visible_text="Extracción vieja.", observations=[], uncertainties=[])

    monkeypatch.setattr(PrivateVision, "extract", edit_during_inference)
    response = client.post(
        url + "/analyze", json={"revision": 1, "confirmed": True}, headers=author
    )
    assert response.status_code == 409
    assert client.get(url, headers=author).json()["body"] == "Cambio concurrente."


def test_parameters_are_required_for_analysis_and_review_but_not_image_storage(
    client: TestClient,
) -> None:
    author = staff(client)
    report = upload(client, author, parameters={}, application={}).json()
    url = f"/v1/clinical/test-images/{report['report_id']}"
    assert report["parameters_ready"] is False
    assert (
        client.post(
            url + "/analyze", json={"revision": 1, "confirmed": True}, headers=author
        ).status_code
        == 422
    )
    assert (
        client.patch(
            url,
            json={"revision": 1, "body": "Texto sin referencias.", "reviewed": True},
            headers=author,
        ).status_code
        == 422
    )
    saved = client.patch(
        url,
        json={"revision": 1, "parameters": PARAMETERS, "application": APPLICATION},
        headers=author,
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["parameters_ready"] and saved.json()["body"] == ""
    assert saved.json()["status"] == "uploaded"
    assert "Edición ficticia 2026" in saved.json()["parameter_summary"]
    assert "30 años, 2 meses" in saved.json()["parameter_summary"]
    assert (
        client.patch(
            url, json={"revision": 1, "parameters": {"version": "obsoleto"}}, headers=author
        ).status_code
        == 409
    )
    final = client.patch(
        url,
        json={"revision": 2, "body": "Informe revisado con sus referencias.", "reviewed": True},
        headers=author,
    )
    assert final.status_code == 200
    assert final.json()["parameters"] == PARAMETERS and final.json()["application"] == APPLICATION
    assert (
        client.patch(
            url, json={"revision": 3, "parameters": {"source": "otra"}}, headers=author
        ).status_code
        == 409
    )
    assert client.get(url, headers=author).json()["parameters"] == PARAMETERS


@pytest.mark.parametrize(
    "parameters,application",
    [
        ({"criteria": "x" * 4001}, {}),
        ({"unexpected": "value"}, {}),
        ({}, {"age_years": -1}),
        ({}, {"age_months": 12, "age_years": 10}),
        ({}, {"age_months": 3}),
        ({}, {"applied_on": "fecha inválida"}),
    ],
)
def test_invalid_parameters_are_rejected_before_storing_image(
    client: TestClient,
    parameters: dict[str, Any],
    application: dict[str, Any],
) -> None:
    author = staff(client)
    assert upload(client, author, parameters=parameters, application=application).status_code == 422
    assert client.get("/v1/clinical/patients/p-image/test-images", headers=author).json() == []


def test_parameter_changes_stay_private_and_preserve_manual_report(client: TestClient) -> None:
    author, other = staff(client), staff(client)
    report = upload(client, author).json()
    url = f"/v1/clinical/test-images/{report['report_id']}"
    denied = client.patch(
        url,
        json={"revision": 1, "parameters": PARAMETERS, "application": APPLICATION},
        headers=other,
    )
    assert denied.status_code == 404
    manual = client.patch(
        url, json={"revision": 1, "body": "Borrador escrito."}, headers=author
    ).json()
    new_params = {**PARAMETERS, "scoring_rules": "Regla actualizada de prueba."}
    saved = client.patch(
        url, json={"revision": manual["revision"], "parameters": new_params}, headers=author
    ).json()
    assert saved["body"] == "Borrador escrito." and saved["application"] == APPLICATION
    assert "Regla actualizada de prueba" in saved["parameter_summary"]
    partial = client.patch(
        url,
        json={
            "revision": saved["revision"],
            "parameters": {"source": "Otra referencia ficticia."},
            "application": {"context": "Contexto actualizado."},
        },
        headers=author,
    )
    assert partial.status_code == 200, partial.text
    assert partial.json()["parameters"]["criteria"] == PARAMETERS["criteria"]
    assert partial.json()["application"]["age_years"] == APPLICATION["age_years"]
    assert partial.json()["application"]["applied_on"] == APPLICATION["applied_on"]
    audit = client.get("/v1/audit", headers=ADMIN).text
    assert "Regla actualizada de prueba" not in audit


def test_parameters_migration_preserves_reports_created_before_parameters() -> None:
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import create_engine, inspect, text

    migrations = Path(__file__).parents[1] / "src/orchestrator/migrations/versions"

    def load(name: str) -> Any:
        spec = importlib.util.spec_from_file_location(name, migrations / (name + ".py"))
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    old = load("0026_test_image_reports")
    change = load("0027_test_image_parameters")
    engine = create_engine("sqlite:///:memory:")
    with (
        engine.begin() as connection,
        Operations.context(MigrationContext.configure(connection)),
    ):
        old.upgrade()
        connection.execute(
            text(
                "INSERT INTO test_image_reports "
                "(tenant,report_id,patient_id,file_id,author_id,title,kind,status,"
                "revision,body,created_at) VALUES ('synthetic','report','patient',"
                "'file','author','Test','drawing','draft',1,'Conservar texto','2026-11-03')"
            )
        )
        change.upgrade()
        assert {"parameters", "application"} <= {
            column["name"] for column in inspect(connection).get_columns("test_image_reports")
        }
        saved = connection.execute(
            text("SELECT body, parameters, application FROM test_image_reports")
        ).one()
        assert tuple(saved) == ("Conservar texto", None, None)
        change.downgrade()
        assert connection.scalar(text("SELECT body FROM test_image_reports")) == "Conservar texto"
    engine.dispose()
