"""Edge behaviour behind the HTTPS gateway (ADR 0016): security headers, no caching of
API answers, HSTS when configured, and CORS only for the exact origins configured."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from orchestrator.api.app import create_app
from orchestrator.catalog import Catalog
from orchestrator.config import Settings
from orchestrator.llm import FakeLLM
from orchestrator.service import Orchestrator

KEY = {"X-API-Key": "test-key"}
APP_ORIGIN = "https://app.consultorio-demo.ec"


def client_for(settings: Settings, catalog: Catalog) -> TestClient:
    return TestClient(create_app(settings, Orchestrator(settings, catalog=catalog, llm=FakeLLM())))


@pytest.fixture
def client(settings: Settings, catalog: Catalog) -> Iterator[TestClient]:
    with client_for(settings, catalog) as c:
        yield c


def test_every_response_carries_the_security_headers(client: TestClient) -> None:
    for path in ("/healthz", "/v1/agents", "/"):
        headers = client.get(path, headers=KEY).headers
        assert headers["X-Content-Type-Options"] == "nosniff"
        assert headers["Referrer-Policy"] == "no-referrer"
        assert headers["X-Frame-Options"] == "DENY"
    assert "Strict-Transport-Security" not in client.get("/healthz").headers  # not configured


def test_api_answers_are_never_cached(client: TestClient) -> None:
    assert client.get("/v1/agents", headers=KEY).headers["Cache-Control"] == "no-store"
    assert "no-store" not in client.get("/healthz").headers.get("Cache-Control", "")


def test_hsts_when_configured(settings: Settings, catalog: Catalog) -> None:
    settings.hsts_max_age_seconds = 31_536_000
    with client_for(settings, catalog) as c:
        assert (
            c.get("/healthz").headers["Strict-Transport-Security"]
            == "max-age=31536000; includeSubDomains"
        )


def test_no_cors_by_default(client: TestClient) -> None:
    preflight = client.options(
        "/v1/agents",
        headers={"Origin": APP_ORIGIN, "Access-Control-Request-Method": "GET"},
    )
    assert "access-control-allow-origin" not in preflight.headers
    plain = client.get("/v1/agents", headers={**KEY, "Origin": APP_ORIGIN})
    assert "access-control-allow-origin" not in plain.headers


def test_cors_only_for_the_configured_origin(settings: Settings, catalog: Catalog) -> None:
    settings.cors_allowed_origins = [APP_ORIGIN]
    with client_for(settings, catalog) as c:
        ok = c.options(
            "/v1/agents",
            headers={
                "Origin": APP_ORIGIN,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "X-API-Key, Content-Type",
            },
        )
        assert ok.status_code == 200
        assert ok.headers["access-control-allow-origin"] == APP_ORIGIN
        assert "access-control-allow-credentials" not in ok.headers
        other = c.get("/v1/agents", headers={**KEY, "Origin": "https://evil.example"})
        assert "access-control-allow-origin" not in other.headers


@pytest.mark.parametrize(
    "origin",
    [
        "*",
        "https://*.example.com",
        "http://evil.example",
        "https://app.example/path",
        "app.example",
    ],
)
def test_bad_origins_are_rejected(origin: str) -> None:
    with pytest.raises(ValidationError, match="invalid CORS origin"):
        Settings(_env_file=None, cors_allowed_origins=[origin])  # type: ignore[call-arg]


def test_plain_http_origins_are_a_production_problem() -> None:
    dev = Settings(_env_file=None, cors_allowed_origins=["http://localhost:5173"])  # type: ignore[call-arg]
    assert dev.production_problems() == []
    prod = Settings(  # type: ignore[call-arg]
        _env_file=None, app_env="prod", cors_allowed_origins=["http://localhost:5173"]
    )
    assert any("plain-HTTP origins" in p for p in prod.production_problems())
