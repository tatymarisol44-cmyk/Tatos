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


def test_gke_gateway_serves_https_and_redirects_http() -> None:
    """ADR 0016: the only public entry is HTTPS; plain HTTP gets a permanent redirect."""
    docs = {(d["kind"], d["metadata"]["name"]): d for d in _docs(GKE / "gateway.yaml")}
    gateway = docs[("Gateway", "agency-orchestrator")]
    assert gateway["spec"]["gatewayClassName"] == "gke-l7-global-external-managed"
    assert gateway["metadata"]["annotations"]["networking.gke.io/certmap"]
    listeners = {
        (lst["name"], lst["protocol"], lst["port"]) for lst in gateway["spec"]["listeners"]
    }
    assert listeners == {("http", "HTTP", 80), ("https", "HTTPS", 443)}

    redirect = docs[("HTTPRoute", "agency-orchestrator-redirect")]["spec"]
    assert redirect["parentRefs"] == [{"name": "agency-orchestrator", "sectionName": "http"}]
    (rule,) = redirect["rules"]
    assert "backendRefs" not in rule  # plain HTTP never reaches the API
    assert rule["filters"] == [
        {"type": "RequestRedirect", "requestRedirect": {"scheme": "https", "statusCode": 301}}
    ]
    api = docs[("HTTPRoute", "agency-orchestrator")]["spec"]
    assert api["parentRefs"] == [{"name": "agency-orchestrator", "sectionName": "https"}]
    assert api["rules"][0]["backendRefs"] == [{"name": "agency-orchestrator", "port": 80}]


def test_gke_overlay_turns_on_hsts() -> None:
    kustomization = _docs(GKE / "kustomization.yaml")[0]
    assert "gateway.yaml" in kustomization["resources"]
    patches = " ".join(p["patch"] for p in kustomization["patches"])
    assert "HSTS_MAX_AGE_SECONDS" in patches and "31536000" in patches


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
            if match and not match.group(1).startswith("./"):  # local: same commit
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
    names = {r["alert"] for g in rules["groups"] for r in g["rules"] if "alert" in r}
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
    # main -> rehearsal -> staging -> approval -> production, the same digest throughout.
    jobs = flow["jobs"]
    assert jobs["staging"]["needs"] == ["resolve", "rehearsal"]
    assert jobs["production"]["needs"] == ["resolve", "staging"]
    for env in ("staging", "production"):
        assert jobs[env]["with"]["environment"] == env
        assert jobs[env]["with"]["image"] == "${{ needs.resolve.outputs.api }}"
    gke = yaml.safe_load((WORKFLOWS / "deploy-gke.yml").read_text(encoding="utf-8"))
    deploy = gke["jobs"]["deploy"]
    assert deploy["environment"] == "${{ inputs.environment }}"  # reviewers gate production
    runs = " ".join(str(s.get("run", "")) for s in deploy["steps"])
    uses = [str(s.get("uses", "")) for s in deploy["steps"]]
    assert "cosign verify" in runs  # only images CI signed reach a cluster
    assert any(u.startswith("google-github-actions/auth@") for u in uses)  # keyless
    assert "KUBECONFIG" not in (WORKFLOWS / "deploy-gke.yml").read_text(encoding="utf-8")
    assert "rollout.sh agency" in runs
    script = (ROOT / "deploy" / "scripts" / "rollout.sh").read_text(encoding="utf-8")
    assert "rollout undo" in script and "rollout status" in script
    strategy = _k8s("Deployment", "agency-orchestrator")["spec"]["strategy"]
    assert strategy["rollingUpdate"]["maxUnavailable"] == 0  # old pods serve meanwhile


def test_the_rehearsal_has_every_secret_prod_requires() -> None:
    # The kind rehearsal runs APP_ENV=prod: a secret prod demands but the rehearsal lacks
    # would crash the pods in CI, not in production first.
    ci = (ROOT / "deploy" / "k8s" / "overlays" / "ci" / "kustomization.yaml").read_text("utf-8")
    for name in ("API_KEYS", "DATABASE_URL", "POSTGRES_URL", "PSEUDONYM_KEY"):
        assert f"- {name}=" in ci, name
    doc = (ROOT / "deploy" / "k8s" / "base" / "deployment.yaml").read_text("utf-8")
    assert "PSEUDONYM_KEY" in doc  # the operator's instructions name it too


def test_the_rehearsal_api_may_reach_every_in_cluster_dependency() -> None:
    # The namespace denies all traffic; the rehearsal's in-cluster Postgres once had no
    # rule, so the migrate init container never connected and the first deploy hung.
    docs = [d for p in K8S.glob("*.yaml") for d in _docs(p)]
    docs += _docs(ROOT / "deploy" / "k8s" / "overlays" / "ci" / "postgres.yaml")
    policies = [d for d in docs if d.get("kind") == "NetworkPolicy"]
    api = {"app.kubernetes.io/name": "agency-orchestrator"}

    def selects(policy: dict[str, Any], labels: dict[str, str]) -> bool:
        wanted = policy["spec"]["podSelector"].get("matchLabels", {})
        return all(labels.get(k) == v for k, v in wanted.items())

    def allows(
        direction: str, peers: str, own: dict[str, str], other: dict[str, str], port: int
    ) -> bool:
        return any(
            selects(p, own)
            and any(
                any(
                    peer.get("podSelector", {}).get("matchLabels") == other
                    for peer in r.get(peers, [])
                )
                and any(pt["port"] == port for pt in r.get("ports", []))
                for r in p["spec"].get(direction, [])
            )
            for p in policies
        )

    for name, port in (("postgres", 5432), ("qdrant", 6333), ("redis", 6379)):
        target = {"app.kubernetes.io/name": name}
        ingress = allows("ingress", "from", target, api, port)
        egress = allows("egress", "to", api, target, port)
        assert ingress and egress, f"API cannot reach {name}:{port} in the rehearsal"


def test_the_rehearsal_postgres_owns_its_data_directory() -> None:
    # An emptyDir mount point belongs to root; Postgres (uid 70, no capabilities) cannot
    # chmod it and initdb crash-loops. PGDATA must be a subdirectory Postgres creates.
    docs = _docs(ROOT / "deploy" / "k8s" / "overlays" / "ci" / "postgres.yaml")
    pod = next(d for d in docs if d.get("kind") == "Deployment")["spec"]["template"]["spec"]
    container = pod["containers"][0]
    env = {e["name"]: e.get("value") for e in container["env"]}
    mounts = {m["name"]: m["mountPath"] for m in container["volumeMounts"]}
    pgdata = env.get("PGDATA", "/var/lib/postgresql/data")
    assert pgdata.startswith(mounts["data"].rstrip("/") + "/"), pgdata


def test_failures_are_readable_without_signing_in() -> None:
    ci = (WORKFLOWS / "ci.yml").read_text(encoding="utf-8")
    assert "scripts/ci/junit_annotations.py junit.xml" in ci
    deploy = (WORKFLOWS / "deploy.yml").read_text(encoding="utf-8")
    assert "deploy/scripts/diagnose.sh agency" in deploy


def test_junit_failures_become_annotations(tmp_path: Path) -> None:
    import subprocess
    import sys

    report = tmp_path / "junit.xml"
    report.write_text(
        '<testsuites><testsuite><testcase classname="tests.test_x" name="test_ok"/>'
        '<testcase classname="tests.test_x" name="test_bad">'
        '<failure message="assert 1 == 2">line one\n100% wrong</failure></testcase>'
        "</testsuite></testsuites>",
        encoding="utf-8",
    )
    out = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "ci" / "junit_annotations.py"), str(report)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "::error title=tests.test_x::test_bad::assert 1 == 2%0Aline one%0A100%25 wrong" in out
    assert "1 failed test(s)" in out


def test_gke_runs_only_mirrored_and_attested_images() -> None:
    # Decisions O4 and O6: every image is copied by digest into the project's Artifact
    # Registry and attested for Binary Authorization by the deploy job, after the CI
    # signature is verified. The cluster refuses anything else, even `kubectl set image`.
    gke = yaml.safe_load((WORKFLOWS / "deploy-gke.yml").read_text(encoding="utf-8"))
    # PyYAML follows YAML 1.1, which reads the key `on` as the boolean True.
    assert {"image", "jvm_image"} <= set(gke[True]["workflow_call"]["inputs"])
    steps = gke["jobs"]["deploy"]["steps"]
    runs = [str(s.get("run", "")) for s in steps]
    order = {
        k: next(i for i, r in enumerate(runs) if k in r)
        for k in (
            "cosign verify",
            "crane copy",
            "binauthz attestations sign-and-create",
            "rollout.sh agency",
        )
    }
    assert order["cosign verify"] < order["crane copy"]
    assert order["crane copy"] < order["binauthz attestations sign-and-create"]
    assert order["binauthz attestations sign-and-create"] < order["rollout.sh agency"]
    apply = next(r for r in runs if "kustomize edit set image" in r)
    for name in ("agency-orchestrator", "jvm-agent", "qdrant/qdrant", "redis"):
        assert f'"{name}=' in apply, name

    flow = yaml.safe_load((WORKFLOWS / "deploy.yml").read_text(encoding="utf-8"))
    for env in ("staging", "production"):
        job = flow["jobs"][env]
        assert job["with"]["jvm_image"] == "${{ needs.resolve.outputs.jvm }}"
        assert job["permissions"]["packages"] == "read"  # crane reads GHCR

    tf = "".join(p.read_text("utf-8") for p in (ROOT / "deploy" / "terraform").glob("*.tf"))
    tf = re.sub(r"\s+", " ", tf)
    assert 'resource "google_artifact_registry_repository"' in tf
    assert 'evaluation_mode = "PROJECT_SINGLETON_POLICY_ENFORCE"' in tf
    assert 'evaluation_mode = "REQUIRE_ATTESTATION"' in tf
    assert 'enforcement_mode = "ENFORCED_BLOCK_AND_AUDIT_LOG"' in tf


def test_rollout_moves_the_cronjobs_with_the_api() -> None:
    # The retention and audit-anchor jobs run the API image: they must not lag a release.
    script = (ROOT / "deploy" / "scripts" / "rollout.sh").read_text(encoding="utf-8")
    assert "set image cronjob" in script
