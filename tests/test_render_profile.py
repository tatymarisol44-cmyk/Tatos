"""The free managed production profile (ADR 0022): the Render Blueprint is the production
profile with no escape hatch, and deploy/render/deploy.sh's contract runs for real against
a fake `docker` and `curl` (digest only, signature first, followed until live). CI also
runs the image with the free instance's limits and walks every flow (deploy.yml, job
`render`)."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

from orchestrator.config import Settings

ROOT = Path(__file__).resolve().parents[1]
RENDER = ROOT / "deploy" / "render"
GOOD = "ghcr.io/x/agency-orchestrator@sha256:" + "a" * 64
NEW = "ghcr.io/x/agency-orchestrator@sha256:" + "b" * 64
# What the owner types in the dashboard, shaped like the real values.
OWNER = {
    "DATABASE_URL": "postgresql+psycopg://postgres.ref:pw@pooler.example:5432/postgres?sslmode=require",
    "POSTGRES_URL": "postgresql://postgres.ref:pw@pooler.example:5432/postgres?sslmode=require",
    "REDIS_URL": "rediss://default:pw@x.upstash.io:6379",
    "QDRANT_URL": "https://x.us-east-1-0.aws.cloud.qdrant.io:6333",
    "QDRANT_API_KEY": "qk",
    "API_KEYS": "svc_x:consultorio",
    "TENANT_PACKS": '{"consultorio":"ec-psychologist"}',
    "PRACTICE_DISPLAY_NAME": "Consultorio",
    "PUBLIC_BASE_URL": "https://agency-api.onrender.com",
    "LLM_BACKEND": "fake",
}
SUPABASE_FREE_POOL = 15  # server connections the session pooler lends on the free plan


def _service() -> dict[str, Any]:
    services = yaml.safe_load((RENDER / "render.yaml").read_text(encoding="utf-8"))["services"]
    assert len(services) == 1
    return services[0]


def _env() -> list[dict[str, Any]]:
    return _service()["envVars"]


def test_the_service_is_free_gated_on_readiness_and_runs_our_image() -> None:
    svc = _service()
    assert svc["type"] == "web" and svc["runtime"] == "image" and svc["plan"] == "free"
    assert svc["healthCheckPath"] == "/readyz"
    install = (ROOT / "deploy" / "single-node" / "install.sh").read_text(encoding="utf-8")
    repo = svc["image"]["url"].rsplit(":", 1)[0]
    assert f'REPO_IMAGE="{repo}"' in install  # the image CI signs, not another one


def test_every_variable_is_a_setting_and_no_secret_is_committed() -> None:
    fields = {name.upper() for name in Settings.model_fields}
    for var in _env():
        # Settings ignores unknown variables: a typo would silently drop a production rule.
        assert var["key"] in fields | {"PORT"}, var["key"]
        assert ("value" in var) + bool(var.get("generateValue")) + (var.get("sync") is False) == 1
    by_key = {v["key"]: v for v in _env()}
    for secret in ("DATABASE_URL", "POSTGRES_URL", "REDIS_URL", "QDRANT_API_KEY", "API_KEYS"):
        assert by_key[secret].get("sync") is False, secret  # typed in the dashboard
    for generated in ("PSEUDONYM_KEY", "META_APP_SECRET", "WHATSAPP_VERIFY_TOKEN"):
        assert by_key[generated].get("generateValue") is True, generated
    assert "ANTHROPIC_API_KEY" not in by_key  # added in the dashboard after the DPA (L8)


def test_the_blueprint_is_a_production_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in _env():
        if "value" in var:
            monkeypatch.setenv(var["key"], var["value"])
        elif var.get("generateValue"):
            monkeypatch.setenv(var["key"], "x" * 44)
    for key, value in OWNER.items():
        monkeypatch.setenv(key, value)
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.app_env == "prod"
    assert settings.production_problems() == []
    assert settings.tenant_keys() == {"svc_x": "consultorio"}
    assert settings.pseudonym_secret()
    assert settings.embedding_backend == "hashing"  # no message text leaves for routing
    # The API's pool plus the checkpointer's must fit the pooler's server connections.
    api = settings.postgres_pool_size + settings.postgres_max_overflow
    assert api + settings.postgres_pool_size <= SUPABASE_FREE_POOL


def test_the_rehearsal_gives_a_value_to_everything_the_owner_types() -> None:
    script = (RENDER / "rehearse.sh").read_text(encoding="utf-8")
    owner_keys = {v["key"] for v in _env() if v.get("sync") is False}
    assert owner_keys == set(OWNER)
    for key in owner_keys:
        assert f"\n{key}=" in script, key
    assert "--memory 512m --memory-swap 512m --cpus 0.1" in script  # the free instance


def test_no_escape_hatch_from_the_production_rules() -> None:
    for path in RENDER.iterdir():
        if not path.is_file():
            continue  # __pycache__ of setup.py
        text = path.read_text(encoding="utf-8")
        for hatch in ("PROD_ALLOW_EPHEMERAL", "POSTGRES_ALLOW_INSECURE", "sslmode=disable"):
            assert hatch not in text, (path.name, hatch)


# --- deploy.sh, executed ---------------------------------------------------------------

FAKE_DOCKER = r"""#!/usr/bin/env bash
echo "docker $*" >> "$CALLS"
case "$*" in
  "run --rm "*cosign*" verify "*) [[ "$*" == *"$UNSIGNED"* ]] && exit 1; exit 0 ;;
  *) exit 0 ;;
esac
"""

# Render's API: POST creates deploy dep-1; each status read takes the next line of
# $STATUSES (the last one repeats); /readyz answers unless READY_FAILS is set.
FAKE_CURL = r"""#!/usr/bin/env bash
echo "curl $*" >> "$CALLS"
case "$*" in
  *"-X POST"*) [ -n "$REJECT" ] && exit 22; echo '{"id":"dep-1","status":"created"}' ;;
  *"/deploys/dep-1"*)
    status="$(head -1 "$STATUSES")"
    [ "$(wc -l < "$STATUSES")" -gt 1 ] && sed -i '1d' "$STATUSES"
    echo "{\"id\":\"dep-1\",\"status\":\"$status\"}" ;;
  *"/readyz"*) [ -z "$READY_FAILS" ] ;;
  *) exit 22 ;;
esac
"""


def _bash() -> str | None:
    found = shutil.which("bash")
    if sys.platform == "win32" and found and "system32" in found.lower():
        return None  # WSL's launcher, not a POSIX bash for this tree
    return found


@pytest.fixture
def stage(tmp_path: Path) -> Path:
    if _bash() is None or shutil.which("jq") is None:
        pytest.skip("needs bash and jq")
    for rel in ("render/deploy.sh", "single-node/deploy.sh"):
        dest = tmp_path / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / "deploy" / rel, dest)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (("docker", FAKE_DOCKER), ("curl", FAKE_CURL)):
        path = bin_dir / name
        path.write_text(body, "utf-8", newline="\n")
        path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return tmp_path


def deploy(
    stage: Path, image: str, statuses: tuple[str, ...] = ("live",), timeout: int = 5, **env: str
) -> subprocess.CompletedProcess[str]:
    (stage / "statuses").write_text("".join(f"{s}\n" for s in statuses), "utf-8", newline="\n")
    run_env = {
        **os.environ,
        "PATH": f"{stage / 'bin'}{os.pathsep}{os.environ['PATH']}",
        "CALLS": str(stage / "calls"),
        "STATUSES": str(stage / "statuses"),
        "RENDER_API_KEY": "rnd_test",
        "RENDER_SERVICE_ID": "srv-test",
        "POLL_SECONDS": "0",
        "UNSIGNED": "__none__",
        "REJECT": "",
        "READY_FAILS": "",
        **env,
    }
    run_env.pop("GITHUB_ACTIONS", None)
    bash = _bash()
    assert bash
    return subprocess.run(
        [bash, str(stage / "render" / "deploy.sh"), image, str(timeout)],
        env=run_env,
        capture_output=True,
        text=True,
        check=False,
    )


def _calls(stage: Path) -> list[str]:
    path = stage / "calls"
    return path.read_text("utf-8").splitlines() if path.exists() else []


def test_a_release_is_followed_until_live(stage: Path) -> None:
    done = deploy(
        stage, NEW, ("created", "update_in_progress", "live"), PUBLIC_URL="https://a.onrender.com"
    )
    assert done.returncode == 0, done.stderr
    calls = _calls(stage)
    verify = next(i for i, c in enumerate(calls) if "verify" in c)
    post = next(i for i, c in enumerate(calls) if "-X POST" in c)
    assert verify < post  # the signature before anything reaches Render
    assert f'"imageUrl":"{NEW}"' in calls[post].replace(" ", "")
    assert "Authorization: Bearer rnd_test" in calls[post]
    assert any("https://a.onrender.com/readyz" in c for c in calls)


@pytest.mark.parametrize("ending", ["update_failed", "build_failed", "canceled"])
def test_a_failed_release_fails_the_deploy(stage: Path, ending: str) -> None:
    done = deploy(stage, NEW, ("update_in_progress", ending))
    assert done.returncode == 1 and ending in done.stderr


def test_a_release_that_never_goes_live_times_out(stage: Path) -> None:
    done = deploy(stage, NEW, ("update_in_progress",), timeout=1)
    assert done.returncode == 1 and "after 1s" in done.stderr


def test_an_unsigned_image_never_reaches_render(stage: Path) -> None:
    done = deploy(stage, NEW, UNSIGNED=NEW)
    assert done.returncode == 1 and "not signed" in done.stderr
    assert not any("api.render.com" in c for c in _calls(stage))


def test_a_rejected_request_or_a_silent_entrance_fails(stage: Path) -> None:
    assert deploy(stage, NEW, REJECT="1").returncode == 1
    silent = deploy(stage, NEW, PUBLIC_URL="https://a.onrender.com", READY_FAILS="1")
    assert silent.returncode == 1 and "readyz" in silent.stderr


def test_only_a_digest_is_deployed(stage: Path) -> None:
    done = deploy(stage, "ghcr.io/x/agency-orchestrator:latest")
    assert done.returncode == 2 and "digest" in done.stderr
    assert _calls(stage) == []  # refused before touching anything
