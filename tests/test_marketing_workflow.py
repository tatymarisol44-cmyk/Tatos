"""Existing catalog → persisted plan → rendered files → review → mock Graph → evidence."""

import hashlib
import io
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from orchestrator.api.app import create_app
from orchestrator.catalog import Catalog, parse_agent
from orchestrator.config import Settings
from orchestrator.db import utcnow
from orchestrator.llm import FakeLLM
from orchestrator.service import Orchestrator
from tests.test_publishing import ADMIN, GLOBEX, connect, instagram, live, staff_key

BRIEF = {
    "objective": "Dar a conocer los servicios públicos del consultorio",
    "audience": "Público general",
    "network": "instagram",
}


def real_specialists(catalog: Catalog) -> Catalog:
    """Read pinned existing definitions, never create replacement test agents."""
    root = Path(__file__).resolve().parents[1] / "vendor" / "agency-agents"
    for division, name in [
        ("marketing", "marketing-content-creator"),
        ("design", "design-visual-storyteller"),
    ]:
        path = root / division / (name + ".md")
        assert path.is_file(), "Initialize the pinned agency-agents submodule for this test."
        spec = parse_agent(path, division)
        assert spec is not None
        catalog.agents[spec.id] = spec
    return catalog


@pytest.fixture
def workflow_app(
    settings: Settings, catalog: Catalog, tmp_path: Path
) -> Iterator[tuple[TestClient, Orchestrator]]:
    settings.media_dir = tmp_path / "media"
    settings.tenant_packs = {"acme": "ec-psychologist"}
    settings.publish_poll_seconds = 0
    orch = Orchestrator(settings, catalog=real_specialists(catalog), llm=FakeLLM())
    with TestClient(create_app(settings, orch)) as client:
        yield client, orch


def plan(client: TestClient) -> dict[str, Any]:
    result = client.post(
        "/v1/marketing/workflows",
        headers=ADMIN,
        json={"brief": BRIEF, "first": utcnow().date().isoformat()},
    )
    assert result.status_code == 201, result.text
    return result.json()


def test_existing_agents_create_verified_resources_and_mock_publishing(
    workflow_app: tuple[TestClient, Orchestrator],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, orch = workflow_app
    calls = live(orch, instagram(), monkeypatch, "instagram")
    brand = client.put(
        "/v1/marketing/brand",
        headers=ADMIN,
        json={
            "version": 0,
            "brand": {
                "name": "Consultorio Ficticio",
                "services": ["Consulta de orientación"],
                "background": "#FFFFFF",
                "accent": "#006666",
                "text": "#222222",
            },
        },
    )
    assert brand.status_code == 200
    out = io.BytesIO()
    Image.new("RGB", (140, 80), "red").save(out, format="PNG")
    assert (
        client.put(
            "/v1/marketing/brand/logo?version=1",
            headers=ADMIN,
            files={"file": ("logo.png", out.getvalue(), "image/png")},
        ).status_code
        == 200
    )
    flow = plan(client)
    assert flow["status"] == "awaiting_review" and flow["payload"]["simulation"]
    assert flow["payload"]["marketing_agent"] == "marketing-content-creator"
    assert flow["payload"]["design_agent"] == "design-visual-storyteller"
    assert flow["resources"] == []
    account = connect(client)
    rendered = client.post(
        f"/v1/marketing/workflows/{flow['id']}/render",
        headers=ADMIN,
        json={
            "revision": flow["revision"],
            "account_id": account,
            "formats": ["feed", "story", "square"],
        },
    )
    assert rendered.status_code == 200, rendered.text
    result = rendered.json()
    assert result["status"] == "ready" and len(result["resources"]) == 3
    for resource in result["resources"]:
        pub = resource["publication"]
        assert pub["status"] == "pending_approval"
        preview = client.get(f"/v1/marketing/creative/{pub['publication_id']}", headers=ADMIN)
        assert preview.status_code == 200
        assert hashlib.sha256(preview.content).hexdigest() == pub["sha256"]
        image = Image.open(io.BytesIO(preview.content))
        assert image.size[0] == 1080
        assert image.getpixel((100, 60))[0] > 230  # actual red logo pixels, not a prompt claim
    assert calls == []  # rendering made no Graph requests
    first = result["resources"][0]["publication_id"]
    base = f"/v1/social/publications/{first}"
    assert client.post(base + "/publish", headers=ADMIN).status_code == 409
    assert client.post(base + "/approve", headers=ADMIN).status_code == 200
    assert (
        client.put(
            base + "/schedule",
            headers=ADMIN,
            json={"at": (utcnow() + timedelta(days=1)).isoformat()},
        ).status_code
        == 200
    )
    assert client.post(base + "/publish", headers=ADMIN).json()["status"] == "published"
    assert len(calls) == 3
    assert client.post(base + "/publish", headers=ADMIN).status_code == 409
    analysis = client.get(f"/v1/marketing/workflows/{flow['id']}/analysis", headers=ADMIN).json()
    assert analysis["published"] == 1 and analysis["external_metrics"]["status"] == "blocked"
    optimized = client.post(
        f"/v1/marketing/workflows/{flow['id']}/optimize",
        headers=ADMIN,
        json={"revision": result["revision"]},
    )
    assert optimized.status_code == 200, optimized.text
    proposal = optimized.json()["payload"]["optimization"]
    assert proposal["source"]["published"] == 1 and proposal["requires_review"]
    audit = client.get("/v1/audit", headers=ADMIN).json()
    events = [e for e in audit if e["details"].get("correlation_id") == flow["id"]]
    assert {e["details"]["tool"] for e in events} >= {"PublicationService.create", "LLM.structured"}
    assert sum(e["action"] == "marketing.workflow.rendered" for e in events) == 3
    assert client.get("/v1/audit/verify", headers=ADMIN).json()["ok"]


def test_identity_and_workflow_are_isolated_and_roles_enforced(
    workflow_app: tuple[TestClient, Orchestrator],
) -> None:
    client, _ = workflow_app
    flow = plan(client)
    reception = staff_key(client, "reception")
    assert client.get("/v1/marketing/workflows", headers=GLOBEX).json() == []
    assert client.get(f"/v1/marketing/workflows/{flow['id']}", headers=GLOBEX).status_code == 404
    assert client.get("/v1/marketing/workflows", headers=reception).status_code == 403
    assert client.get("/v1/marketing/brand", headers=reception).status_code == 403
    account = connect(client)
    assert (
        client.post(
            f"/v1/marketing/workflows/{flow['id']}/render",
            headers=GLOBEX,
            json={"revision": flow["revision"], "account_id": account},
        ).status_code
        == 404
    )
    assert client.get("/v1/marketing/brand/logo", headers=GLOBEX).status_code == 404


def test_plan_revision_invalid_logo_and_failed_agent_do_not_create_creatives(
    workflow_app: tuple[TestClient, Orchestrator],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, orch = workflow_app
    flow = plan(client)
    body = {
        "revision": flow["revision"],
        "draft": flow["payload"]["draft"],
        "plan": flow["payload"]["plan"],
    }
    body["draft"]["title"] = "Título revisado por una persona"
    path = f"/v1/marketing/workflows/{flow['id']}"
    assert client.put(path, headers=ADMIN, json=body).status_code == 200
    assert client.put(path, headers=ADMIN, json=body).status_code == 409
    assert (
        client.put(
            "/v1/marketing/brand/logo?version=0",
            headers=ADMIN,
            files={"file": ("bad.svg", b"<svg/>", "image/svg+xml")},
        ).status_code
        == 409
    )
    assert client.get("/v1/social/publications", headers=ADMIN).json() == []

    async def invalid(*args: Any, **kwargs: Any) -> Any:
        from orchestrator.llm import LLMResult

        return LLMResult("invalid JSON", "synthetic")

    monkeypatch.setattr(orch.marketing_designer.llm, "complete", invalid)
    failed = client.post(
        "/v1/marketing/workflows",
        headers=ADMIN,
        json={"brief": BRIEF, "first": utcnow().date().isoformat()},
    )
    assert failed.status_code == 409
    states = client.get("/v1/marketing/workflows", headers=ADMIN).json()
    assert states[0]["status"] == "failed"
    assert states[0]["resources"] == []


def test_resume_after_a_stored_file_was_not_attached_does_not_duplicate_it(
    workflow_app: tuple[TestClient, Orchestrator],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from orchestrator.creatives import CreativeUnavailable

    client, orch = workflow_app
    flow, account = plan(client), connect(client)
    original = orch.publications.create
    interrupted = False

    async def crash_after_storage(*args: Any, **kwargs: Any) -> Any:
        nonlocal interrupted
        resource = await original(*args, **kwargs)
        if not interrupted:
            interrupted = True
            raise CreativeUnavailable("synthetic interruption after persistence")
        return resource

    monkeypatch.setattr(orch.publications, "create", crash_after_storage)
    url = f"/v1/marketing/workflows/{flow['id']}/render"
    body = {"revision": flow["revision"], "account_id": account, "formats": ["feed", "story"]}
    assert client.post(url, headers=ADMIN, json=body).status_code == 503
    before = client.get("/v1/social/publications", headers=ADMIN).json()
    assert len(before) == 1
    failed = client.get(f"/v1/marketing/workflows/{flow['id']}", headers=ADMIN).json()
    assert failed["status"] == "render_failed" and failed["resources"] == []
    body["revision"] = failed["revision"]
    retry = client.post(url, headers=ADMIN, json=body)
    assert retry.status_code == 200, retry.text
    files = client.get("/v1/social/publications", headers=ADMIN).json()
    assert len(files) == 2
    assert (
        next(p["sha256"] for p in files if p["publication_id"] == before[0]["publication_id"])
        == before[0]["sha256"]
    )
    assert all(p["status"] == "pending_approval" for p in files)
