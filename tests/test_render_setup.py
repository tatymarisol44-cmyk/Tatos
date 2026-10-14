"""deploy/render/setup.py (ADR 0022): the owner's private file becomes the Blueprint's
environment, the managed services are checked first, Render is driven through its API, a
re-run keeps the generated secrets, and no secret is ever printed."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("render_setup", ROOT / "deploy/render/setup.py")
assert _spec and _spec.loader
setup = importlib.util.module_from_spec(_spec)
sys.modules["render_setup"] = setup
_spec.loader.exec_module(setup)

PASSWORD = "p@ss/w:rd#1"  # every character a URL would misread
POOLER = "postgresql://postgres.abcdref:[YOUR-PASSWORD]@aws-0-us-east-1.pooler.supabase.com:5432/postgres"
FILE = {
    "RENDER_API_KEY": "rnd_secret_api_key",
    "SUPABASE_POOLER_URL": POOLER,
    "SUPABASE_PASSWORD": PASSWORD,
    "UPSTASH_ENDPOINT": "eager-owl-1234.upstash.io",
    "UPSTASH_PASSWORD": "upstash_secret_pw",
    "QDRANT_URL": "https://abc-123.us-east-1-0.aws.cloud.qdrant.io",
    "QDRANT_API_KEY": "qdrant_secret_key",
    "PRACTICE_ID": "consultorio",
    "PRACTICE_NAME": "Consultorio Andino",
}
SECRETS = ("rnd_secret_api_key", PASSWORD, "upstash_secret_pw", "qdrant_secret_key")


def _write(tmp_path: Path, **override: str) -> Path:
    path = tmp_path / "agency-render.env"
    lines = ["# private"] + [f"{k}={v}" for k, v in {**FILE, **override}.items()]
    path.write_text("\n".join(lines) + "\nSERVICE_KEY=\n", encoding="utf-8")
    return path


def test_supabase_session_pooler_string_becomes_both_urls() -> None:
    database, postgres = setup.database_urls(POOLER, PASSWORD)
    tail = "postgres.abcdref:p%40ss%2Fw%3Ard%231@aws-0-us-east-1.pooler.supabase.com:5432"
    # The server's name and chain are verified against Supabase's root in the image.
    tls = "sslmode=verify-full&sslrootcert=/app/certs/supabase-root-2021.crt"
    assert database == f"postgresql+psycopg://{tail}/postgres?{tls}"
    assert postgres == f"postgresql://{tail}/postgres?{tls}"
    # From the owner's computer: the repo's copy, path encoded (spaces, drive letters).
    _, local = setup.database_urls(POOLER, PASSWORD, "C:\\My Docs\\ca.crt")
    assert local.endswith("sslrootcert=C%3A%5CMy%20Docs%5Cca.crt")


def test_the_supabase_root_is_the_verified_one() -> None:
    # Fingerprint checked against the live pooler and an independent copy (deploy/certs).
    import hashlib
    import ssl

    pem = setup.LOCAL_SUPABASE_CA.read_text(encoding="ascii")
    der = ssl.PEM_cert_to_DER_cert(pem)
    assert hashlib.sha256(der).hexdigest().upper() == (
        "807025AD50D4ED219D2C9C7D299C004F824EB00CF7F65AFEF607D07B72E6CAFA"
    )
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "COPY deploy/certs /app/certs" in dockerfile
    assert "!deploy/certs" in (ROOT / ".dockerignore").read_text(encoding="utf-8")
    assert f"/app/certs/{setup.LOCAL_SUPABASE_CA.name}" == setup.IMAGE_SUPABASE_CA


@pytest.mark.parametrize(
    "wrong",
    [
        POOLER.replace(":5432", ":6543"),  # transaction pooler: no session for checkpoints
        "postgresql://postgres:x@db.abcdref.supabase.co:5432/postgres",  # direct: IPv6 only
        POOLER.replace("postgres.abcdref", "postgres"),
        "https://abcdref.supabase.co",
    ],
)
def test_anything_but_the_session_pooler_is_refused(wrong: str) -> None:
    with pytest.raises(setup.SetupError):
        setup.database_urls(wrong, PASSWORD)


@pytest.mark.parametrize(
    "endpoint",
    [
        "eager-owl-1234.upstash.io",
        "rediss://default:old@eager-owl-1234.upstash.io:6379",
        "https://eager-owl-1234.upstash.io",  # the REST URL, pasted by mistake
    ],
)
def test_upstash_endpoint_becomes_a_verified_tls_url(endpoint: str) -> None:
    url = setup.redis_url(endpoint, "a/b")
    assert url == "rediss://default:a%2Fb@eager-owl-1234.upstash.io:6379"
    with pytest.raises(setup.SetupError):
        setup.redis_url("localhost", "x")


def test_qdrant_endpoint_gets_its_port_and_must_be_https() -> None:
    assert setup.qdrant_url("abc.cloud.qdrant.io") == "https://abc.cloud.qdrant.io:6333"
    assert (
        setup.qdrant_url("https://abc.cloud.qdrant.io:6333") == "https://abc.cloud.qdrant.io:6333"
    )
    with pytest.raises(setup.SetupError):
        setup.qdrant_url("http://abc.cloud.qdrant.io")


def test_the_file_is_checked_and_the_service_key_generated_once(tmp_path: Path) -> None:
    with pytest.raises(setup.SetupError, match="PRACTICE_ID"):
        setup.read_values(_write(tmp_path, PRACTICE_ID="mi consultorio"))
    with pytest.raises(setup.SetupError, match="QDRANT_API_KEY") as missing:
        setup.read_values(_write(tmp_path, QDRANT_API_KEY=""))
    assert not any(s in str(missing.value) for s in SECRETS)
    path = _write(tmp_path)
    values = setup.read_values(path)
    assert setup.keep_service_key(path, values) is True
    key = values["SERVICE_KEY"]
    assert key.startswith("svc_") and len(key) == 52
    again = setup.read_values(path)
    assert setup.keep_service_key(path, again) is False and again["SERVICE_KEY"] == key


def test_the_environment_is_the_blueprints() -> None:
    values = {**FILE, "SERVICE_KEY": "svc_k"}
    env = setup.env_vars(setup.blueprint(), setup.owner_values(values, "https://x.onrender.com"))
    by_key = {v["key"]: v for v in env}
    assert len(by_key) == len(setup.blueprint()["envVars"])
    assert by_key["APP_ENV"]["value"] == "prod"
    assert by_key["PSEUDONYM_KEY"] == {"key": "PSEUDONYM_KEY", "generateValue": True}
    assert by_key["API_KEYS"]["value"] == "svc_k:consultorio"
    assert json.loads(by_key["TENANT_PACKS"]["value"]) == {"consultorio": "ec-psychologist"}
    assert by_key["LLM_BACKEND"]["value"] == "fake"  # until Anthropic's DPA is signed
    assert by_key["REDIS_URL"]["value"].startswith("rediss://")


# --- Render, through a fake API -------------------------------------------------------------


class FakeRender:
    def __init__(self, existing: bool = False) -> None:
        self.calls: list[tuple[str, str, Any]] = []
        self.env = {"PSEUDONYM_KEY": "kept-pseudonym-key"} if existing else {}
        self.existing = existing

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        self.calls.append((request.method, request.url.path, body))
        assert request.headers["Authorization"] == "Bearer rnd_secret_api_key"
        path = request.url.path.removeprefix("/v1")
        svc = {"id": "srv-1", "name": "agency-api", "serviceDetails": {"url": URL}}
        if (request.method, path) == ("GET", "/services"):
            return httpx.Response(200, json=[{"service": svc}] if self.existing else [])
        if (request.method, path) == ("GET", "/owners"):
            return httpx.Response(200, json=[{"owner": {"id": "tea-1", "name": "Taty"}}])
        if (request.method, path) == ("POST", "/services"):
            return httpx.Response(201, json={"service": svc, "deployId": "dep-0"})
        if (request.method, path) == ("GET", "/services/srv-1/env-vars"):
            rows = [{"envVar": {"key": k, "value": v}} for k, v in self.env.items()]
            return httpx.Response(200, json=rows)
        if request.method == "PUT":
            return httpx.Response(200, json=[])
        if (request.method, path) == ("POST", "/services/srv-1/deploys"):
            if not self.existing:  # right after creation the first deploy is running:
                return httpx.Response(202)  # Render queues this one and says nothing more
            return httpx.Response(201, json={"id": "dep-2"})
        if (request.method, path) == ("GET", "/services/srv-1/deploys"):
            return httpx.Response(200, json=[{"deploy": {"id": "dep-2", "status": "queued"}}])
        if request.method == "GET" and path.startswith("/services/srv-1/deploys/"):
            return httpx.Response(200, json={"id": path.rsplit("/", 1)[1], "status": "live"})
        return httpx.Response(404, json={"message": "unexpected"})

    def client(self) -> Any:
        return setup.Render(
            "rnd_secret_api_key",
            httpx.Client(base_url=setup.RENDER_API, transport=httpx.MockTransport(self.handler)),
        )


URL = "https://agency-api-x1y2.onrender.com"  # Render added a suffix


@pytest.fixture
def services_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(setup, "CHECKS", {"Supabase": lambda owner: "ok"})
    monkeypatch.setattr(
        setup.httpx, "get", lambda url, **kw: httpx.Response(200, request=httpx.Request("GET", url))
    )


@pytest.mark.usefixtures("services_answer")
def test_first_run_creates_the_free_service_and_prints_no_secret(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeRender()
    assert setup.main([str(_write(tmp_path))], render=fake.client(), poll=0) == 0
    create = next(
        body for method, path, body in fake.calls if (method, path) == ("POST", "/v1/services")
    )
    assert create["serviceDetails"]["plan"] == "free"
    assert create["serviceDetails"]["region"] == "virginia"
    assert create["serviceDetails"]["healthCheckPath"] == "/readyz"
    assert create["image"]["imagePath"].startswith("ghcr.io/tatymarisol44-cmyk/agency-orchestrator")
    env = {v["key"]: v for v in create["envVars"]}
    assert env["PSEUDONYM_KEY"].get("generateValue") is True
    # The real address is written back, then that release is the one followed.
    assert ("PUT", "/v1/services/srv-1/env-vars/PUBLIC_BASE_URL", {"value": URL}) in fake.calls
    followed = [path for method, path, _ in fake.calls if "/deploys/" in path]
    assert followed[-1] == "/v1/services/srv-1/deploys/dep-2"
    out = capsys.readouterr()
    printed = out.out + out.err
    service_key = setup.read_values(tmp_path / "agency-render.env")["SERVICE_KEY"]
    assert not any(s in printed for s in (*SECRETS, service_key))
    assert URL in printed and "srv-1" in printed


@pytest.mark.usefixtures("services_answer")
def test_a_rerun_updates_and_keeps_the_generated_secrets(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeRender(existing=True)
    secrets_file = _write(tmp_path)
    assert setup.main([str(secrets_file)], render=fake.client(), poll=0) == 0
    # The pseudonym key is copied to the private file (to read a restored backup), and
    # never printed.
    assert "PSEUDONYM_KEY=kept-pseudonym-key" in secrets_file.read_text(encoding="utf-8")
    assert "kept-pseudonym-key" not in capsys.readouterr().out
    put = next(
        body for method, path, body in fake.calls if path.endswith("/env-vars") and method == "PUT"
    )
    env = {v["key"]: v for v in put}
    assert env["PSEUDONYM_KEY"] == {"key": "PSEUDONYM_KEY", "value": "kept-pseudonym-key"}
    assert env["META_APP_SECRET"] == {"key": "META_APP_SECRET", "generateValue": True}
    assert env["PUBLIC_BASE_URL"]["value"] == URL
    assert not any(method == "POST" and path == "/v1/services" for method, path, _ in fake.calls)


def test_a_failing_service_stops_before_render_and_hides_the_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def refuses(owner: dict[str, str]) -> str:
        raise RuntimeError(f"password authentication failed for {owner['POSTGRES_URL']}")

    monkeypatch.setattr(setup, "CHECKS", {"Supabase": refuses})
    fake = FakeRender()
    assert setup.main([str(_write(tmp_path))], render=fake.client(), poll=0) == 1
    assert fake.calls == []
    out = capsys.readouterr().out
    assert "FAIL  Supabase" in out and "***" in out
    assert not any(s in out for s in SECRETS)


@pytest.mark.usefixtures("services_answer")
def test_check_only_changes_nothing(tmp_path: Path) -> None:
    fake = FakeRender()
    assert setup.main([str(_write(tmp_path)), "--check-only"], render=fake.client()) == 0
    assert fake.calls == []
