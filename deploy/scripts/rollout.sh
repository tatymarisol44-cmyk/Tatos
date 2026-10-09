#!/usr/bin/env bash
# Health-gated deploy with automatic rollback (second audit: "CD with health check and
# rollback"). Used by .github/workflows/deploy.yml for the kind rehearsal on every push
# to main and for production.
#
#   rollout.sh <namespace> <image> [timeout]
#
# 1. Sets the API image (and its migrate init container) to <image>. The Deployment's
#    strategy is maxUnavailable 0: old pods keep serving until new ones are Ready.
# 2. Waits for the rollout. A pod becomes Ready only when the schema is migrated and
#    /readyz answers (catalog indexed, databases reachable).
# 3. Smoke test through the Service: /readyz and an authenticated /v1/agents.
# 4. The scheduled jobs (retention, audit anchor) run the API image: they move with it,
#    only once the release is healthy.
# Any failure: `kubectl rollout undo` to the previous ReplicaSet, wait until that is
# healthy again, and exit 1 so the pipeline fails loudly.
set -euo pipefail
ns="${1:?usage: rollout.sh <namespace> <image> [timeout]}"
image="${2:?usage: rollout.sh <namespace> <image> [timeout]}"
timeout="${3:-300s}"
deploy="deploy/agency-orchestrator"
smoke_key="${SMOKE_API_KEY:-}"

smoke() {
  local port=18080 pf ok=1
  kubectl -n "$ns" port-forward svc/agency-orchestrator "$port:80" >/dev/null 2>&1 &
  pf=$!
  for _ in $(seq 1 "${SMOKE_TRIES:-30}"); do
    if curl -fsS "http://127.0.0.1:$port/readyz" >/dev/null 2>&1; then ok=0; break; fi
    sleep 1
  done
  if [ "$ok" -eq 0 ] && [ -n "$smoke_key" ]; then
    curl -fsS -H "X-API-Key: $smoke_key" "http://127.0.0.1:$port/v1/agents" >/dev/null || ok=1
  fi
  kill "$pf" 2>/dev/null || true
  return "$ok"
}

rollback() {
  echo "::error::rollout of $image failed: rolling back"
  kubectl -n "$ns" rollout undo "$deploy"
  kubectl -n "$ns" rollout status "$deploy" --timeout="$timeout"
  smoke || echo "::error::service unhealthy even after the rollback"
  exit 1
}

previous="$(kubectl -n "$ns" get "$deploy" -o jsonpath='{.spec.template.spec.containers[0].image}')"
echo "current: $previous"
echo "deploying: $image"
kubectl -n "$ns" set image "$deploy" api="$image" migrate="$image"
kubectl -n "$ns" rollout status "$deploy" --timeout="$timeout" || rollback
smoke || rollback
kubectl -n "$ns" set image cronjob -l app.kubernetes.io/name=agency-retention "*=$image"
echo "deployed $image"
