"""Regression tests for the deployment configuration (external audit, production step).

These read the files that ship (Dockerfiles, compose, Kubernetes, workflows) and pin the
properties the audit asked for, so a later edit cannot silently undo them."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
K8S = ROOT / "deploy" / "k8s" / "base"
WORKFLOWS = ROOT / ".github" / "workflows"
DIGEST = re.compile(r"@sha256:[0-9a-f]{64}$")


def _docs(path: Path) -> list[dict[str, Any]]:
    return [d for d in yaml.safe_load_all(path.read_text(encoding="utf-8")) if d]


def _k8s(kind: str, name: str) -> dict[str, Any]:
    for path in K8S.glob("*.yaml"):
        for doc in _docs(path):
            if doc.get("kind") == kind and doc["metadata"]["name"] == name:
                return doc
    raise AssertionError(f"{kind}/{name} not found")


GKE = ROOT / "deploy" / "k8s" / "overlays" / "gke"


def test_base_blocks_cloud_metadata_for_every_workload() -> None:
    for name in ("api-egress", "jvm-agent-egress"):
        for rule in _k8s("NetworkPolicy", name)["spec"]["egress"]:
            for peer in rule.get("to", []):
                block = peer.get("ipBlock")
                if block and block["cidr"] == "0.0.0.0/0":
                    assert {"169.254.169.254/32", "169.254.170.2/32"} <= set(block["except"])


def test_overlays_that_add_resources_set_the_namespace() -> None:
    """A base's `namespace:` does not reach resources an overlay adds; without its own, they
    land in `default` (this broke the CI overlay's Postgres and Secret, and the GKE overlay's
    ServiceAccount, before it was caught)."""
    base_ns = _docs(K8S / "kustomization.yaml")[0]["namespace"]
    for path in (ROOT / "deploy" / "k8s" / "overlays").glob("*/kustomization.yaml"):
        kustomization = _docs(path)[0]
        adds = [r for r in kustomization.get("resources", []) if not r.startswith("../")]
        if adds or kustomization.get("secretGenerator") or kustomization.get("configMapGenerator"):
            assert kustomization.get("namespace") == base_ns, path.parent.name


def test_gke_overlay_opens_only_the_gke_metadata_server_to_the_api() -> None:
    """Keyless credentials (owner's decision A8): exactly the two endpoints Google documents,
    for the API pods only."""
    (policy,) = _docs(GKE / "networkpolicy-gke-metadata.yaml")
    spec = policy["spec"]
    assert spec["podSelector"] == {"matchLabels": {"app.kubernetes.io/name": "agency-orchestrator"}}
    assert spec["policyTypes"] == ["Egress"]
    opened = {(r["to"][0]["ipBlock"]["cidr"], r["ports"][0]["port"]) for r in spec["egress"]}
    assert opened == {("169.254.169.252/32", 988), ("169.254.169.254/32", 80)}
    assert all(len(r["to"]) == 1 and len(r["ports"]) == 1 for r in spec["egress"])


def test_gke_overlay_runs_the_api_as_its_own_service_account() -> None:
    (account,) = _docs(GKE / "serviceaccount.yaml")
    assert account["metadata"]["name"] == "agency-orchestrator"
    assert account["automountServiceAccountToken"] is False
    kustomization = _docs(GKE / "kustomization.yaml")[0]
    assert {"serviceaccount.yaml", "networkpolicy-gke-metadata.yaml"} <= set(
        kustomization["resources"]
    )
    patch = next(p for p in kustomization["patches"] if p["target"]["kind"] == "Deployment")
    assert "serviceAccountName" in patch["patch"] and "agency-orchestrator" in patch["patch"]


def test_media_dir_is_on_a_writable_mount() -> None:
    """The root filesystem is read-only; rendered creatives must go under a mounted /tmp."""
    media_dir = _k8s("ConfigMap", "agency-orchestrator-config")["data"]["MEDIA_DIR"]
    api = _k8s("Deployment", "agency-orchestrator")["spec"]["template"]["spec"]["containers"][0]
    assert api["securityContext"]["readOnlyRootFilesystem"] is True
    mounts = [m["mountPath"] for m in api["volumeMounts"]]
    assert any(media_dir == m or media_dir.startswith(m.rstrip("/") + "/") for m in mounts)


def test_base_images_are_pinned_by_digest() -> None:
    dockerfiles = [ROOT / "Dockerfile", ROOT / "agents" / "jvm-specialist" / "Dockerfile"]
    for path in dockerfiles:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("FROM "):
                assert DIGEST.search(line.split()[1]), f"{path.name}: {line}"
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    for name, svc in compose["services"].items():
        if "image" in svc:
            assert DIGEST.search(svc["image"]), name
    for path in K8S.glob("*.yaml"):
        for doc in _docs(path):
            pod = doc.get("spec", {}).get("template", {}).get("spec", {})
            for container in pod.get("containers", []):
                image = container["image"]
                # Our own images are pinned by kustomize `images:`; third-party ones by digest.
                if "/" in image or ":" in image:
                    assert DIGEST.search(image), f"{path.name}: {image}"


def test_compose_publishes_ports_on_localhost_only() -> None:
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    for name, svc in compose["services"].items():
        for port in svc.get("ports", []):
            assert str(port).startswith("127.0.0.1:"), f"{name}: {port}"


def test_replicated_api_uses_the_shared_rate_limiter() -> None:
    replicas = _k8s("Deployment", "agency-orchestrator")["spec"]["replicas"]
    max_replicas = _k8s("HorizontalPodAutoscaler", "agency-orchestrator")["spec"]["maxReplicas"]
    assert max(replicas, max_replicas) > 1
    config = _k8s("ConfigMap", "agency-orchestrator-config")["data"]
    assert config["RATE_LIMIT_BACKEND"] == "redis"
    assert config["REDIS_URL"].startswith("redis://redis:")
    _k8s("Service", "redis")


def test_namespace_denies_by_default_and_api_egress_is_scoped() -> None:
    deny = _k8s("NetworkPolicy", "default-deny-all")["spec"]
    assert deny["podSelector"] == {}
    assert set(deny["policyTypes"]) == {"Ingress", "Egress"}
    assert "ingress" not in deny and "egress" not in deny
    egress = _k8s("NetworkPolicy", "api-egress")["spec"]["egress"]
    for rule in egress:
        assert rule.get("ports"), "every egress rule names its ports"
        for peer in rule["to"]:
            if "ipBlock" in peer:
                assert "169.254.169.254/32" in peer["ipBlock"]["except"]


def test_workflow_actions_are_pinned_to_commit_shas() -> None:
    for path in WORKFLOWS.glob("*.yml"):
        for line in path.read_text(encoding="utf-8").splitlines():
            match = re.search(r"uses:\s*(\S+)", line)
            if match:
                assert re.search(r"@[0-9a-f]{40}$", match.group(1)), f"{path.name}: {line}"


def test_images_are_scanned_before_anything_is_pushed() -> None:
    ci = yaml.safe_load((WORKFLOWS / "ci.yml").read_text(encoding="utf-8"))
    steps = ci["jobs"]["image"]["steps"]

    def is_push(step: dict[str, Any]) -> bool:
        return "build-push-action" in step.get("uses", "") and step["with"].get("push") is True

    def is_scan(step: dict[str, Any]) -> bool:
        return "trivy-action" in step.get("uses", "")

    first_push = next(i for i, s in enumerate(steps) if is_push(s))
    scans = [i for i, s in enumerate(steps) if is_scan(s)]
    assert len(scans) == 2
    assert all(i < first_push for i in scans)
    for step in steps[:first_push]:
        if "build-push-action" in step.get("uses", ""):
            assert step["with"]["push"] is False and step["with"]["load"] is True


def test_retention_runs_on_a_schedule_and_alerts() -> None:
    # A31: retention is scheduled, not left to an operator, and its failure pages.
    job = _k8s("CronJob", "agency-retention")
    pod = job["spec"]["jobTemplate"]["spec"]["template"]
    assert pod["spec"]["containers"][0]["command"] == ["agency", "retention"]
    assert job["spec"]["concurrencyPolicy"] == "Forbid"
    # Not selected by the API Service (it must not receive traffic).
    api = _k8s("Service", "agency-orchestrator")["spec"]["selector"]
    assert pod["metadata"]["labels"]["app.kubernetes.io/name"] != api["app.kubernetes.io/name"]
    rules = yaml.safe_load((ROOT / "deploy" / "monitoring" / "alerts.yml").read_text("utf-8"))
    names = {r["alert"] for g in rules["groups"] for r in g["rules"]}
    assert {"RetentionJobFailed", "RetentionJobNotRunning"} <= names


def test_schema_is_migrated_before_the_api_starts() -> None:
    # A33: an init container runs the migrations; the API only checks the revision.
    pod = _k8s("Deployment", "agency-orchestrator")["spec"]["template"]["spec"]
    [init] = pod["initContainers"]
    assert init["command"] == ["agency", "db", "upgrade"]
    config = _k8s("ConfigMap", "agency-orchestrator-config")["data"]
    assert config.get("DB_AUTO_MIGRATE", "false") == "false"


def test_deploys_are_health_gated_and_roll_back() -> None:
    # Second audit: CD with a health check and rollback, rehearsed on every push.
    flow = yaml.safe_load((WORKFLOWS / "deploy.yml").read_text(encoding="utf-8"))
    steps = " ".join(str(s.get("run", "")) for s in flow["jobs"]["rehearsal"]["steps"])
    assert "rollout.sh agency agency-orchestrator:ci" in steps
    assert "does-not-exist" in steps  # a broken release is rehearsed too
    assert flow["jobs"]["production"]["environment"] == "production"
    script = (ROOT / "deploy" / "scripts" / "rollout.sh").read_text(encoding="utf-8")
    assert "rollout undo" in script and "rollout status" in script
    strategy = _k8s("Deployment", "agency-orchestrator")["spec"]["strategy"]
    assert strategy["rollingUpdate"]["maxUnavailable"] == 0  # old pods serve meanwhile
