"""Observability wiring (docs/slo.md): every alert has a runbook, the GKE rules are the
rendered alerts.yml, and the metrics port is reachable by Prometheus only."""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
ALERTS = ROOT / "deploy" / "monitoring" / "alerts.yml"
K8S = ROOT / "deploy" / "k8s"


def _docs(path: Path) -> list[dict[str, Any]]:
    return [d for d in yaml.safe_load_all(path.read_text(encoding="utf-8")) if d]


def _alerts() -> list[dict[str, Any]]:
    groups = yaml.safe_load(ALERTS.read_text(encoding="utf-8"))["groups"]
    return [r for g in groups for r in g["rules"] if "alert" in r]


def test_every_alert_has_a_severity_and_an_existing_runbook() -> None:
    alerts = _alerts()
    assert len(alerts) >= 10
    for rule in alerts:
        assert rule["labels"]["severity"] in {"page", "ticket"}, rule["alert"]
        runbook = rule["annotations"].get("runbook")
        assert runbook, f"{rule['alert']} has no runbook"
        assert (ROOT / runbook).is_file(), f"{rule['alert']}: {runbook} is missing"


def test_the_runbook_index_lists_existing_pages_and_every_alert() -> None:
    index = (ROOT / "docs" / "runbooks" / "README.md").read_text(encoding="utf-8")
    for page in re.findall(r"\]\(([\w-]+\.md)\)", index):
        assert (ROOT / "docs" / "runbooks" / page).is_file(), page
    for rule in _alerts():
        assert rule["alert"] in index, f"{rule['alert']} is not in the runbook index"


def test_gke_rules_are_the_rendered_alerts() -> None:
    spec = importlib.util.spec_from_file_location(
        "render_gmp_rules", ROOT / "deploy" / "monitoring" / "render_gmp_rules.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    committed = (K8S / "overlays" / "gke" / "rules.yaml").read_text(encoding="utf-8")
    assert committed == module.render(), "run deploy/monitoring/render_gmp_rules.py"


def test_metrics_port_is_internal_and_scraped() -> None:
    deployment = _docs(K8S / "base" / "deployment.yaml")[0]
    [api] = [c for c in deployment["spec"]["template"]["spec"]["containers"] if c["name"] == "api"]
    ports = {p["name"]: p["containerPort"] for p in api["ports"]}
    config = _docs(K8S / "base" / "configmap.yaml")[0]["data"]
    assert ports["metrics"] == int(config["METRICS_PORT"]) == 9464
    # The Service (and so the Gateway) exposes the API port only.
    service = _docs(K8S / "base" / "service.yaml")[0]
    assert all(p.get("targetPort") != "metrics" for p in service["spec"]["ports"])
    assert all(p["port"] != 9464 for p in service["spec"]["ports"])
    # Only the Prometheus collectors' namespaces may reach 9464.
    policies = _docs(K8S / "base" / "networkpolicy.yaml")
    [ingress] = [d for d in policies if d["metadata"]["name"] == "api-ingress"]
    [metrics_rule] = [r for r in ingress["spec"]["ingress"] if r["ports"] == [{"port": 9464}]]
    allowed = {
        f["namespaceSelector"]["matchLabels"]["kubernetes.io/metadata.name"]
        for f in metrics_rule["from"]
    }
    assert allowed == {"gmp-system", "observability"}
    monitor = _docs(K8S / "overlays" / "gke" / "podmonitoring.yaml")[0]
    assert monitor["spec"]["endpoints"][0]["port"] == "metrics"


def _alertmanager() -> Any:
    spec = importlib.util.spec_from_file_location(
        "render_alertmanager", ROOT / "deploy" / "monitoring" / "render_alertmanager.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


PAGING_ENV = {"TELEGRAM_BOT_TOKEN": "123:abc", "TELEGRAM_CHAT_ID": "-1001"}
EMAIL_ENV = {
    "ALERT_EMAIL_TO": "ops@example.test",
    "SMTP_HOST": "smtp.example.test:587",
    "SMTP_FROM": "alerts@example.test",
    "SMTP_USERNAME": "alerts",
    "SMTP_PASSWORD": "s3cret",
}


def test_alertmanager_pages_on_telegram_and_mails_tickets() -> None:
    # Decision O1: `page` alerts reach the platform on-call on Telegram (and e-mail when
    # configured), repeating hourly until resolved; `ticket` alerts go by e-mail, daily.
    [secret] = yaml.safe_load_all(_alertmanager().render({**PAGING_ENV, **EMAIL_ENV}))
    assert (secret["kind"], secret["metadata"]["namespace"]) == ("Secret", "gmp-public")
    assert secret["metadata"]["name"] == "alertmanager"
    config = yaml.safe_load(secret["stringData"]["alertmanager.yaml"])
    routes = {r["matchers"][0]: r for r in config["route"]["routes"]}
    page = next(
        r for r in config["receivers"] if r["name"] == routes['severity="page"']["receiver"]
    )
    assert page["telegram_configs"][0]["chat_id"] == -1001
    assert page["email_configs"][0]["to"] == "ops@example.test"
    assert page["email_configs"][0]["require_tls"] is True
    assert routes['severity="page"']["repeat_interval"] == "1h"
    ticket = routes['severity="ticket"']
    assert ticket["repeat_interval"] == "24h"
    assert next(r for r in config["receivers"] if r["name"] == ticket["receiver"])["email_configs"]


def test_alertmanager_without_email_still_pages_and_needs_telegram() -> None:
    module = _alertmanager()
    config = yaml.safe_load(
        next(yaml.safe_load_all(module.render(PAGING_ENV)))["stringData"]["alertmanager.yaml"]
    )
    for receiver in config["receivers"]:  # tickets fall back to Telegram: never dropped
        assert receiver.get("telegram_configs"), receiver["name"]
    with pytest.raises(SystemExit, match="TELEGRAM_BOT_TOKEN"):
        module.render({})
