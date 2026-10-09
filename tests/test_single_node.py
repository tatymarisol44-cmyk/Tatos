"""The single-node production profile (ADR 0021): the files that ship, and deploy.sh's
contract run for real against a fake `docker` and `curl` (digest only, signature first,
health-gated, rollback to the previous image). CI also installs the whole stack on a
runner and walks every flow (deploy.yml, job `single-node`)."""

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

ROOT = Path(__file__).resolve().parents[1]
NODE = ROOT / "deploy" / "single-node"
GOOD = "ghcr.io/x/agency-orchestrator@sha256:" + "a" * 64
NEW = "ghcr.io/x/agency-orchestrator@sha256:" + "b" * 64


def _compose() -> dict[str, Any]:
    return yaml.safe_load((NODE / "compose.yaml").read_text(encoding="utf-8"))


def test_every_image_is_pinned_and_nothing_listens_on_the_internet() -> None:
    services = _compose()["services"]
    for name, svc in services.items():
        image = svc["image"]
        assert image == "${API_IMAGE:?set by deploy.sh}" or "@sha256:" in image, name
        for port in svc.get("ports", []):
            assert str(port).startswith("127.0.0.1:"), (name, port)
    assert set(services) >= {"migrate", "api", "postgres", "qdrant", "redis", "tunnel-quick"}


def test_the_api_runs_the_production_profile_hardened() -> None:
    services = _compose()["services"]
    api = services["api"]
    env = api["environment"]
    assert env["APP_ENV"] == "prod"  # the same refusal rules as Kubernetes (A33)
    assert env["CHECKPOINTER_BACKEND"] == "postgres"
    assert env["VECTOR_BACKEND"] == "qdrant" and env["RATE_LIMIT_BACKEND"] == "redis"
    assert api["read_only"] is True and api["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in api["security_opt"]
    assert "readyz" in " ".join(api["healthcheck"]["test"])
    # No exception to the prod rules: Postgres speaks TLS even on one host.
    for url in ("DATABASE_URL", "POSTGRES_URL"):
        assert env[url].endswith("?sslmode=require"), url
    assert "ssl=on" in services["postgres"]["command"]
    # A container that drops every capability cannot switch users itself: it must start
    # as its unprivileged user (CI found redis failing with "setresuid failed").
    for name, svc in services.items():
        if svc.get("cap_drop") == ["ALL"] and not svc["image"].startswith("${API_IMAGE"):
            assert svc.get("user"), f"{name} drops all capabilities but starts as root"
    assert services["migrate"]["profiles"] == ["migrate"]  # never started by `up`
    assert services["migrate"]["command"] == ["agency", "db", "upgrade"]
    for tunnel in ("tunnel", "tunnel-quick"):
        assert services[tunnel]["depends_on"]["api"]["condition"] == "service_healthy"


def test_install_keeps_secrets_private_and_out_of_the_output() -> None:
    script = (NODE / "install.sh").read_text(encoding="utf-8")
    assert "umask 077" in script and 'chmod 600 "$dir/.env"' in script
    assert "openssl rand" in script
    assert "download.docker.com" in script and "signed-by=" in script  # signed apt repo
    assert "curl -fsSL https://get.docker.com" not in script  # no piped installer
    assert 'echo "$service_key"' not in script


def test_backups_are_checked_and_leave_the_server_with_only_their_credentials() -> None:
    script = (NODE / "backup.sh").read_text(encoding="utf-8")
    assert "pg_restore --list" in script and "sha256sum" in script
    assert "agency audit-anchor" in script
    assert "--env-file" not in script  # rclone never sees the rest of .env


# --- deploy.sh, executed ---------------------------------------------------------------

FAKE_DOCKER = r"""#!/usr/bin/env bash
# Records each call; FAIL_<what> makes that step fail for the image in API_IMAGE / args.
echo "docker $*" >> "$CALLS"
img="${API_IMAGE:-$(sed -n 's/^API_IMAGE=//p' .env | tail -1)}"
case "$*" in
  "run --rm "*cosign*" verify "*) [[ "$*" == *"$UNSIGNED"* ]] && exit 1; exit 0 ;;
  "compose pull"*) [[ "$img" == "$MISSING" ]] && exit 1; exit 0 ;;
  "compose --profile migrate run"*) [[ "$img" == "$MIGRATION_FAILS" ]] && exit 1; exit 0 ;;
  "compose up"*) [[ "$img" == "$UNHEALTHY" ]] && exit 1; echo "$img" > "$RUNNING"; exit 0 ;;
  *) exit 0 ;;
esac
"""

FAKE_CURL = r"""#!/usr/bin/env bash
running="$(cat "$RUNNING" 2>/dev/null)"
[[ -n "$running" && "$running" != "$SMOKE_FAILS" ]]
"""


def _bash() -> str | None:
    found = shutil.which("bash")
    if sys.platform == "win32" and found and "system32" in found.lower():
        return None  # WSL's launcher, not a POSIX bash for this tree
    return found


@pytest.fixture
def node(tmp_path: Path) -> Path:
    if _bash() is None:
        pytest.skip("no bash")
    shutil.copy(NODE / "deploy.sh", tmp_path / "deploy.sh")
    (tmp_path / ".env").write_text(f"API_KEYS=svc_x:clinica\nAPI_IMAGE={GOOD}\n", "utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (("docker", FAKE_DOCKER), ("curl", FAKE_CURL)):
        path = bin_dir / name
        path.write_text(body, "utf-8", newline="\n")
        path.chmod(path.stat().st_mode | stat.S_IEXEC)
    (tmp_path / "running").write_text(GOOD, "utf-8")
    return tmp_path


def deploy(node: Path, image: str, **fail: str) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "PATH": f"{node / 'bin'}{os.pathsep}{os.environ['PATH']}",
        "CALLS": str(node / "calls"),
        "RUNNING": str(node / "running"),
        "UNSIGNED": fail.get("unsigned", "__none__"),
        "MISSING": fail.get("missing", "__none__"),
        "UNHEALTHY": fail.get("unhealthy", "__none__"),
        "MIGRATION_FAILS": fail.get("migration", "__none__"),
        "SMOKE_FAILS": fail.get("smoke", "__none__"),
    }
    bash = _bash()
    assert bash
    return subprocess.run(
        [bash, str(node / "deploy.sh"), image, "5"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def running_image(node: Path) -> str:
    env = (node / ".env").read_text("utf-8")
    return [line for line in env.splitlines() if line.startswith("API_IMAGE=")][-1][10:]


def test_a_healthy_release_is_recorded(node: Path) -> None:
    done = deploy(node, NEW)
    assert done.returncode == 0, done.stderr
    assert running_image(node) == NEW
    assert NEW in (node / ".deploy-history").read_text("utf-8")
    calls = (node / "calls").read_text("utf-8").splitlines()
    order = [next(i for i, c in enumerate(calls) if k in c) for k in ("verify", "migrate", "up")]
    assert order == sorted(order)  # signature, then migrations, then the API


@pytest.mark.parametrize(
    "failure",
    [{"unsigned": NEW}, {"missing": NEW}, {"migration": NEW}, {"unhealthy": NEW}, {"smoke": NEW}],
)
def test_any_failure_rolls_back_to_the_running_image(node: Path, failure: dict[str, str]) -> None:
    done = deploy(node, NEW, **failure)
    assert done.returncode == 1
    assert running_image(node) == GOOD
    assert (node / "running").read_text("utf-8").strip() == GOOD
    assert not (node / ".deploy-history").exists()


def test_only_a_digest_is_deployed(node: Path) -> None:
    done = deploy(node, "ghcr.io/x/agency-orchestrator:latest")
    assert done.returncode == 2 and "digest" in done.stderr
    assert not (node / "calls").exists()  # refused before touching anything


def test_no_escape_hatch_from_the_production_rules() -> None:
    for path in NODE.iterdir():
        text = path.read_text(encoding="utf-8")
        for hatch in ("PROD_ALLOW_EPHEMERAL", "POSTGRES_ALLOW_INSECURE"):
            assert hatch not in text, (path.name, hatch)
