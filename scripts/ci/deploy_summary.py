"""Report production deployment separately from successful rehearsals."""

from __future__ import annotations

import os
from pathlib import Path

TARGETS = {"render": "RENDER_RESULT", "vm": "VM_RESULT", "gke": "GKE_RESULT"}


def summarize(target: str, results: dict[str, str]) -> tuple[str, bool]:
    if not target:
        return (
            "Production was NOT deployed: DEPLOY_TARGET is unset. This run covers rehearsals only.",
            True,
        )
    if target not in TARGETS:
        return f"Production was NOT deployed: unsupported DEPLOY_TARGET {target!r}.", False
    result = results.get(TARGETS[target], "unknown")
    if result != "success":
        return f"Production was NOT deployed successfully to {target}: job result {result}.", False
    return f"Production deployment to {target} completed successfully.", True


def main() -> int:
    target = os.environ.get("DEPLOY_TARGET", "").strip()
    message, ok = summarize(target, dict(os.environ))
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with Path(summary).open("a", encoding="utf-8") as report:
            report.write("### Production deployment\n\n" + message + "\n")
    # Do not interpolate untrusted target values into workflow commands.
    if not target:
        print("::warning::No production target configured; only rehearsals were requested.")
    print(message)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
