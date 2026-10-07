"""Publications (ADR 0015, M3): approval before publishing, once only, dry run without a
credential, and the official Instagram and TikTok flows against a simulated network."""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Callable, Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from orchestrator.api.app import create_app
from orchestrator.catalog import Catalog
from orchestrator.config import Settings
from orchestrator.llm import FakeLLM
from orchestrator.media_store import GcsMediaStore, LocalMediaStore, object_name
from orchestrator.service import Orchestrator

ADMIN = {"X-API-Key": "test-key"}  # tenant acme; a service key holds every role
GLOBEX = {"X-API-Key": "other-key"}
TOKEN = "EAAG-synthetic-test-token"
BRIEF = {
    "title": "Cuidar tu mente también es salud",
    "points": ["Hablarlo ayuda.", "Pedir apoyo a tiempo es cuidarte."],
    "cta": "Agenda tu cita en línea",
    "practice_name": "Consultorio Demo",
}
Handler = Callable[[httpx.Request], httpx.Response]


def ffmpeg() -> str | None:
    return os.environ.get("FFMPEG_BINARY") or shutil.which("ffmpeg")


class SignedStore(LocalMediaStore):
    """A local store that can also hand out a URL, as the GCS store does."""

    async def signed_url(self, name: str) -> str | None:
        return f"https://storage.example/{name}?X-Goog-Signature=abc"


@pytest.fixture
def app(
    settings: Settings, catalog: Catalog, tmp_path: Path
) -> Iterator[tuple[TestClient, Orchestrator]]:
    settings.tenant_packs = {"acme": "ec-psychologist"}
    settings.media_dir = tmp_path / "media"
    settings.publish_poll_seconds = 0
    settings.publish_poll_attempts = 2
    if ffmpeg():
        settings.ffmpeg_binary = ffmpeg()  # type: ignore[assignment]
    orch = Orchestrator(settings, catalog=catalog, llm=FakeLLM())
    with TestClient(create_app(settings, orch)) as c:
        yield c, orch


def connect(client: TestClient, network: str = "instagram", **extra: Any) -> str:
    body = {
        "network": network,
        "external_id": f"{network}-178414",
        "handle": "@consultorio.demo",
        "secret_ref": f"{network.upper()}_DEMO",
        **extra,
    }
    response = client.post("/v1/social/accounts", json=body, headers=ADMIN)
    assert response.status_code == 201, response.text
    return str(response.json()["account_id"])


def create(
    client: TestClient, account_id: str, headers: dict[str, str] = ADMIN, **extra: Any
) -> httpx.Response:
    body = {
        "account_id": account_id,
        "kind": "infographic",
        "caption": "Tu bienestar importa.",
        "brief": BRIEF,
        **extra,
    }
    return client.post("/v1/social/publications", json=body, headers=headers)  # type: ignore[return-value]


def route(
    client: TestClient, pub_id: str, action: str, headers: dict[str, str] = ADMIN
) -> httpx.Response:
    return client.post(f"/v1/social/publications/{pub_id}/{action}", headers=headers)  # type: ignore[return-value]


def staff_key(client: TestClient, *roles: str) -> dict[str, str]:
    made = client.post(
        "/v1/admin/staff",
        json={"name": "staff-" + "-".join(roles), "roles": list(roles)},
        headers=ADMIN,
    )
    return {"X-API-Key": made.json()["key"]}


# --- lifecycle --------------------------------------------------------------------------


def test_approve_then_publish_once_in_dry_run(
    app: tuple[TestClient, Orchestrator], settings: Settings
) -> None:
    client, _ = app
    account = connect(client)
    created = create(client, account)
    assert created.status_code == 201, created.text
    pub = created.json()
    assert pub["status"] == "pending_approval" and len(pub["sha256"]) == 64
    assert (settings.media_dir / pub["object_name"]).exists()

    assert route(client, pub["publication_id"], "publish").status_code == 409  # not approved
    assert route(client, pub["publication_id"], "approve").json()["status"] == "approved"
    assert route(client, pub["publication_id"], "approve").status_code == 409

    done = route(client, pub["publication_id"], "publish").json()
    assert done["status"] == "published" and done["mode"] == "dry_run"
    assert done["external_id"].startswith("dry-run-")
    assert "IG-PUBLIC-URL" in done["error"]  # a local store has no URL: it says so
    assert route(client, pub["publication_id"], "publish").status_code == 409  # only once

    audit = client.get("/v1/audit", headers=ADMIN).text
    for action in ("publication.created", "publication.approved", "publication.published"):
        assert action in audit


def test_forbidden_caption_or_creative_stores_nothing(
    app: tuple[TestClient, Orchestrator], settings: Settings
) -> None:
    client, _ = app
    account = connect(client)
    bad_caption = create(client, account, caption="Te ofrecemos la cura definitiva")
    assert bad_caption.status_code == 422
    assert "banned_claim:la cura" in bad_caption.json()["detail"]["violations"]
    bad_brief = create(client, account, brief={**BRIEF, "title": "Curación garantizada"})
    assert bad_brief.status_code == 422
    assert client.get("/v1/social/publications", headers=ADMIN).json() == []
    assert list(settings.media_dir.rglob("*.jpg")) == []  # nothing rendered was kept


def test_networks_and_kinds_that_cannot_publish(app: tuple[TestClient, Orchestrator]) -> None:
    client, _ = app
    assert create(client, connect(client, "tiktok")).status_code == 409  # photos need a domain
    assert create(client, connect(client, "facebook")).status_code == 409  # rules not read
    assert create(client, connect(client, "whatsapp")).status_code == 409  # messages only
    assert create(client, "0123456789ab").status_code == 404  # no such account


def test_a_disabled_account_cannot_publish(app: tuple[TestClient, Orchestrator]) -> None:
    client, _ = app
    account = connect(client)
    client.delete(f"/v1/social/accounts/{account}", headers=ADMIN)
    assert create(client, account).status_code == 409


def test_discounts_above_the_cap_need_the_owner(app: tuple[TestClient, Orchestrator]) -> None:
    client, _ = app
    account = connect(client)
    pub = create(client, account, caption="20% en tu primera cita.").json()
    assert pub["needs_owner_approval"] is True
    marketing = staff_key(client, "marketing")
    assert route(client, pub["publication_id"], "approve", marketing).status_code == 403
    owner = staff_key(client, "owner")
    assert route(client, pub["publication_id"], "approve", owner).json()["status"] == "approved"


def test_roles_and_tenants(app: tuple[TestClient, Orchestrator]) -> None:
    client, _ = app
    account = connect(client)
    reception = staff_key(client, "reception")
    assert create(client, account, reception).status_code == 403
    pub = create(client, account).json()
    assert (
        client.get(
            f"/v1/social/publications/{pub['publication_id']}", headers=reception
        ).status_code
        == 200
    )
    assert (
        client.get(f"/v1/social/publications/{pub['publication_id']}", headers=GLOBEX).status_code
        == 404
    )
    assert route(client, pub["publication_id"], "approve", GLOBEX).status_code == 404


def test_cancel(app: tuple[TestClient, Orchestrator]) -> None:
    client, _ = app
    pub = create(client, connect(client)).json()
    assert route(client, pub["publication_id"], "cancel").json()["status"] == "cancelled"
    assert route(client, pub["publication_id"], "cancel").status_code == 409
    assert route(client, pub["publication_id"], "approve").status_code == 409


# --- live Instagram against a simulated Graph API ---------------------------------------


def live(
    orch: Orchestrator, handler: Handler, monkeypatch: pytest.MonkeyPatch, network: str
) -> list[httpx.Request]:
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    monkeypatch.setenv(f"SOCIAL_SECRET_{network.upper()}_DEMO", TOKEN)
    orch.publications.transport = httpx.MockTransport(record)
    orch.publications.store = SignedStore(orch.settings.media_dir)
    return seen


def instagram(status: str = "FINISHED") -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/media"):
            return httpx.Response(200, json={"id": "container-1"})
        if request.url.path.endswith("/container-1"):
            return httpx.Response(200, json={"status_code": status})
        if request.url.path.endswith("/media_publish"):
            return httpx.Response(200, json={"id": "ig-post-9"})
        return httpx.Response(404)

    return handler


def approved(client: TestClient, account: str, **extra: Any) -> str:
    pub = create(client, account, **extra).json()
    route(client, pub["publication_id"], "approve")
    return str(pub["publication_id"])


def test_instagram_live_flow_keeps_the_token_out_of_urls(
    app: tuple[TestClient, Orchestrator], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, orch = app
    seen = live(orch, instagram(), monkeypatch, "instagram")
    pub_id = approved(client, connect(client))
    done = route(client, pub_id, "publish").json()
    assert done["status"] == "published" and done["mode"] == "live"
    assert done["external_id"] == "ig-post-9" and done["visibility"] == "public"
    assert [r.url.path for r in seen] == [
        "/instagram-178414/media",
        "/container-1",
        "/instagram-178414/media_publish",
    ]
    first = dict(httpx.QueryParams(seen[0].content.decode()))
    assert first["image_url"].startswith("https://storage.example/acme/")
    for request in seen:
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        assert TOKEN not in str(request.url)
    assert TOKEN not in client.get("/v1/audit", headers=ADMIN).text


@pytest.mark.parametrize(
    ("status", "expected"), [("ERROR", "container ERROR"), ("IN_PROGRESS", "not ready")]
)
def test_instagram_container_problems_fail_the_publication(
    app: tuple[TestClient, Orchestrator],
    monkeypatch: pytest.MonkeyPatch,
    status: str,
    expected: str,
) -> None:
    client, orch = app
    live(orch, instagram(status), monkeypatch, "instagram")
    done = route(client, approved(client, connect(client)), "publish").json()
    assert done["status"] == "failed" and expected in done["error"]


def test_platform_http_errors_report_only_the_status(
    app: tuple[TestClient, Orchestrator], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, orch = app
    live(
        orch,
        lambda r: httpx.Response(400, json={"error": {"code": 190, "message": f"bad {TOKEN}"}}),
        monkeypatch,
        "instagram",
    )
    done = route(client, approved(client, connect(client)), "publish").json()
    assert done["status"] == "failed" and done["error"] == "instagram container: HTTP 400 190"


def test_a_transport_error_is_uncertain_not_retried(
    app: tuple[TestClient, Orchestrator], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, orch = app

    def broken(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    live(orch, broken, monkeypatch, "instagram")
    pub_id = approved(client, connect(client))
    done = route(client, pub_id, "publish").json()
    assert done["status"] == "uncertain"
    assert route(client, pub_id, "publish").status_code == 409


def test_live_instagram_without_a_url_fails_before_any_call(
    app: tuple[TestClient, Orchestrator], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, orch = app
    seen = live(orch, instagram(), monkeypatch, "instagram")
    orch.publications.store = LocalMediaStore(orch.settings.media_dir)  # no URL to give
    done = route(client, approved(client, connect(client)), "publish").json()
    assert done["status"] == "failed" and "IG-PUBLIC-URL" in done["error"]
    assert seen == []


# --- live TikTok ------------------------------------------------------------------------


def tiktok(options: list[str]) -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v2/post/publish/creator_info/query/":
            return httpx.Response(
                200, json={"data": {"privacy_level_options": options}, "error": {"code": "ok"}}
            )
        if path == "/v2/post/publish/video/init/":
            return httpx.Response(
                200,
                json={
                    "data": {"publish_id": "v_pub_1", "upload_url": "https://upload.example/u1"},
                    "error": {"code": "ok"},
                },
            )
        if request.url.host == "upload.example":
            return httpx.Response(201)
        return httpx.Response(404)

    return handler


@pytest.mark.skipif(ffmpeg() is None, reason="ffmpeg is not installed")
def test_tiktok_unaudited_posts_privately(
    app: tuple[TestClient, Orchestrator], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, orch = app
    seen = live(orch, tiktok(["SELF_ONLY"]), monkeypatch, "tiktok")
    pub_id = approved(client, connect(client, "tiktok"), kind="video")
    done = route(client, pub_id, "publish").json()
    assert done["status"] == "published" and done["visibility"] == "private"
    assert done["external_id"] == "v_pub_1"
    init = json.loads(seen[1].content)
    assert init["post_info"]["privacy_level"] == "SELF_ONLY"
    size = init["source_info"]["video_size"]
    assert init["source_info"]["total_chunk_count"] == 1
    assert seen[2].headers["Content-Range"] == f"bytes 0-{size - 1}/{size}"
    assert "Authorization" not in seen[2].headers  # the upload URL is pre-authorised


@pytest.mark.skipif(ffmpeg() is None, reason="ffmpeg is not installed")
def test_tiktok_audited_but_public_not_offered_fails(
    app: tuple[TestClient, Orchestrator], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, orch = app
    live(orch, tiktok(["SELF_ONLY"]), monkeypatch, "tiktok")
    pub_id = approved(client, connect(client, "tiktok", audited=True), kind="video")
    done = route(client, pub_id, "publish").json()
    assert done["status"] == "failed" and "PUBLIC_TO_EVERYONE not offered" in done["error"]


# --- media stores -----------------------------------------------------------------------


class FakeBlob:
    def __init__(self, calls: list[tuple[str, Any]], name: str) -> None:
        self.calls, self.name = calls, name

    def upload_from_filename(self, filename: str, content_type: str) -> None:
        self.calls.append(("upload", (self.name, Path(filename).name, content_type)))

    def generate_signed_url(self, **kwargs: Any) -> str:
        self.calls.append(("sign", kwargs))
        return f"https://storage.googleapis.com/bucket/{self.name}?X-Goog-Signature=x"


class FakeBucket:
    def __init__(self, calls: list[tuple[str, Any]]) -> None:
        self.calls = calls

    def blob(self, name: str) -> FakeBlob:
        return FakeBlob(self.calls, name)


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    def bucket(self, name: str) -> FakeBucket:
        self.calls.append(("bucket", name))
        return FakeBucket(self.calls)


async def test_gcs_store_uploads_and_signs_v4_urls_that_expire(tmp_path: Path) -> None:
    client = FakeClient()
    store = GcsMediaStore("creativos", timedelta(minutes=60), client=client, cache_dir=tmp_path)
    source = tmp_path / "in.jpg"
    source.write_bytes(b"\xff\xd8 jpeg")
    await store.put("acme/p1.jpg", source, "image/jpeg")
    url = await store.signed_url("acme/p1.jpg")
    assert url and "X-Goog-Signature" in url
    assert ("upload", ("acme/p1.jpg", "in.jpg", "image/jpeg")) in client.calls
    sign = next(kw for kind, kw in client.calls if kind == "sign")
    assert sign == {"version": "v4", "expiration": timedelta(minutes=60), "method": "GET"}
    assert store.local_path("acme/p1.jpg") is not None  # cached for file uploads


def test_object_names_are_confined(tmp_path: Path) -> None:
    assert object_name("acme", "abc123", ".jpg") == "acme/abc123.jpg"
    for tenant, pub, suffix in [
        ("../x", "a", ".jpg"),
        ("acme", "a/b", ".jpg"),
        ("acme", "a", ".exe"),
    ]:
        with pytest.raises(ValueError):
            object_name(tenant, pub, suffix)
    with pytest.raises(ValueError, match="escapes"):
        LocalMediaStore(tmp_path).local_path("../outside.jpg")
