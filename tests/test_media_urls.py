"""Creatives served by the API itself at HMAC-signed, expiring URLs (for Instagram and
Facebook to fetch when there is no bucket, e.g. in a Codespace)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

from orchestrator.api.app import create_app
from orchestrator.catalog import Catalog
from orchestrator.config import Settings
from orchestrator.llm import FakeLLM
from orchestrator.media_store import LocalMediaStore, build_media_store
from orchestrator.service import Orchestrator

NAME = "acme/pub123.jpg"


@pytest.fixture
def served(
    settings: Settings, catalog: Catalog, tmp_path: Path
) -> Iterator[tuple[TestClient, LocalMediaStore]]:
    settings.media_dir = tmp_path
    settings.public_base_url = "https://demo-8000.app.github.dev"
    (tmp_path / "acme").mkdir()
    (tmp_path / NAME).write_bytes(b"\xff\xd8\xff synthetic jpeg")
    orch = Orchestrator(settings, catalog=catalog, llm=FakeLLM())
    with TestClient(create_app(settings, orch)) as c:
        store = orch.publications.store
        assert isinstance(store, LocalMediaStore)
        yield c, store


async def test_a_signed_url_serves_the_creative(served: tuple[TestClient, LocalMediaStore]) -> None:
    client, store = served
    url = await store.signed_url(NAME)
    assert url and url.startswith("https://demo-8000.app.github.dev/media/acme/pub123.jpg?exp=")
    parts = urlsplit(url)
    got = client.get(f"{parts.path}?{parts.query}")
    assert got.status_code == 200 and got.content.startswith(b"\xff\xd8\xff")
    assert got.headers["content-type"] == "image/jpeg"
    # Forged signature, another file with the same signature, expired: all refused.
    query = dict(p.split("=") for p in parts.query.split("&"))
    assert client.get(f"{parts.path}?exp={query['exp']}&sig={'0' * 64}").status_code == 404
    assert client.get(f"/media/acme/other.jpg?{parts.query}").status_code == 404
    old = int(query["exp"]) - 7200
    assert client.get(f"{parts.path}?exp={old}&sig={query['sig']}").status_code == 404
    # No way out of the media directory, and nothing but jpg/mp4.
    assert client.get(f"/media/../secrets.jpg?{parts.query}").status_code == 404
    assert client.get(f"/media/acme/x.txt?{parts.query}").status_code in (404, 422)


async def test_no_url_without_an_https_public_address(settings: Settings, tmp_path: Path) -> None:
    settings.media_dir = tmp_path
    (tmp_path / "acme").mkdir()
    (tmp_path / NAME).write_bytes(b"x")
    settings.public_base_url = "http://localhost:8000"  # Meta cannot fetch from here
    assert await build_media_store(settings).signed_url(NAME) is None
    settings.public_base_url = "https://demo.example"
    store = build_media_store(settings)
    assert await store.signed_url("acme/missing.jpg") is None  # nothing to serve
    assert isinstance(store, LocalMediaStore) and store.ttl == timedelta(minutes=60)
