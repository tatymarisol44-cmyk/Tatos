"""deploy/scripts/rollout.sh against a fake kubectl and curl: a healthy release stays, a
release that never becomes Ready or fails the smoke test is rolled back and the script
exits 1. The real thing is rehearsed on a kind cluster in .github/workflows/deploy.yml."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BASH = shutil.which("bash")
pytestmark = pytest.mark.skipif(BASH is None, reason="needs bash")

# The fake cluster keeps the current image in a file; "rollout status" fails for any
# image containing "broken"; "rollout undo" restores the previous image.
FAKE_KUBECTL = """#!/usr/bin/env bash
state="$FAKE_STATE"
echo "kubectl $*" >> "$state/log"
case "$*" in
  *"get deploy/agency-orchestrator"*) cat "$state/image" ;;
  *"set image"*)
    cp "$state/image" "$state/previous"
    for a in "$@"; do
      case "$a" in api=*) printf '%s' "${a#api=}" > "$state/image" ;; esac
    done ;;
  *"rollout status"*) grep -q broken "$state/image" && exit 1; exit 0 ;;
  *"rollout undo"*) cp "$state/previous" "$state/image" ;;
  *"port-forward"*) sleep 5 ;;
esac
"""
FAKE_CURL = """#!/usr/bin/env bash
grep -q unhealthy "$FAKE_STATE/image" && exit 22
exit 0
"""


def _run(tmp_path: Path, current: str, image: str) -> tuple[int, str, str]:
    bin_dir, state = tmp_path / "bin", tmp_path / "state"
    bin_dir.mkdir(exist_ok=True)
    state.mkdir(exist_ok=True)
    for name, body in (("kubectl", FAKE_KUBECTL), ("curl", FAKE_CURL)):
        path = bin_dir / name
        path.write_text(body, encoding="utf-8", newline="\n")
        path.chmod(0o755)
    (state / "image").write_text(current, encoding="utf-8")
    (state / "log").write_text("", encoding="utf-8")
    env = {
        **os.environ,
        "PATH": f"{bin_dir.as_posix()}{os.pathsep}{os.environ['PATH']}",
        "FAKE_STATE": state.as_posix(),
        "SMOKE_TRIES": "2",
    }
    script = (ROOT / "deploy" / "scripts" / "rollout.sh").as_posix()
    assert BASH is not None
    done = subprocess.run(
        [BASH, "-c", f'export PATH="{_posix(bin_dir)}:$PATH"; bash "{script}" agency {image} 5s'],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    log = (state / "log").read_text(encoding="utf-8")
    return done.returncode, (state / "image").read_text(encoding="utf-8"), log


def _posix(path: Path) -> str:
    """A path bash on this OS understands (Git Bash on Windows wants /c/...)."""
    text = path.as_posix()
    if len(text) > 1 and text[1] == ":":
        return f"/{text[0].lower()}{text[2:]}"
    return text


def test_healthy_release_stays(tmp_path: Path) -> None:
    code, image, log = _run(tmp_path, "app:v1", "app:v2")
    assert code == 0 and image == "app:v2"
    assert "rollout undo" not in log


def test_release_that_never_gets_ready_is_rolled_back(tmp_path: Path) -> None:
    code, image, log = _run(tmp_path, "app:v1", "app:broken")
    assert code == 1 and image == "app:v1"
    assert "rollout undo" in log


def test_release_that_fails_the_smoke_test_is_rolled_back(tmp_path: Path) -> None:
    code, image, log = _run(tmp_path, "app:v1", "app:unhealthy")
    assert code == 1 and image == "app:v1"
    assert "rollout undo" in log
