"""Containers that keep a deploy from being Ready, one per line: pod TAB container TAB why.

Reads `kubectl get pods -o json` on stdin (used by diagnose.sh).
"""

from __future__ import annotations

import json
import sys


def problems(pods: dict) -> list[tuple[str, str, str]]:
    found = []
    for pod in pods.get("items", []):
        name = pod["metadata"]["name"]
        status = pod.get("status", {})
        if not status.get("containerStatuses") and status.get("phase") == "Pending":
            conditions = [
                f"{c.get('reason', '')} {c.get('message', '')}".strip()
                for c in status.get("conditions", [])
                if c.get("status") != "True"
            ]
            found.append((name, "-", "Pending: " + "; ".join(filter(None, conditions))))
        for c in status.get("initContainerStatuses", []) + status.get("containerStatuses", []):
            state = c.get("state", {})
            done = state.get("terminated")
            if c.get("ready") or (done is not None and done.get("exitCode", 1) == 0):
                continue
            kind, detail = next(iter(state.items()), ("unknown", {}))
            why = f"{kind}: {detail.get('reason', '')} {detail.get('message') or ''}".strip()
            last = c.get("lastState", {}).get("terminated")
            if last:
                why += f" | last exit {last.get('exitCode')} {last.get('reason', '')}"
            found.append((name, c["name"], f"{why} (restarts {c.get('restartCount', 0)})"))
    return found


if __name__ == "__main__":
    for row in problems(json.load(sys.stdin)):
        print("\t".join(row))
