"""Render deploy/monitoring/alerts.yml as a Google Managed Prometheus `ClusterRules`
resource for the gke overlay, so the alert definitions have one source:

    python deploy/monitoring/render_gmp_rules.py > deploy/k8s/overlays/gke/rules.yaml

`tests/test_observability.py` fails when the committed file is out of date."""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
HEADER = """\
# GENERATED from deploy/monitoring/alerts.yml by deploy/monitoring/render_gmp_rules.py.
# Do not edit by hand. Google Managed Prometheus evaluates these rules; notification
# routing (who gets paged, on which channel) is the Alertmanager config of the project
# (owner decision O1 in docs/private/DECISIONS-PENDING.md).
"""


def render() -> str:
    rules = yaml.safe_load((HERE / "alerts.yml").read_text(encoding="utf-8"))
    for group in rules["groups"]:  # GMP requires an explicit evaluation interval
        group.setdefault("interval", "60s")
    resource = {
        "apiVersion": "monitoring.googleapis.com/v1",
        "kind": "ClusterRules",
        "metadata": {"name": "agency-orchestrator"},
        "spec": {"groups": rules["groups"]},
    }
    return HEADER + yaml.safe_dump(resource, sort_keys=False, width=100, allow_unicode=True)


if __name__ == "__main__":
    sys.stdout.write(render())
