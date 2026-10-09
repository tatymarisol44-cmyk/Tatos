#!/usr/bin/env bash
# Deploy one API image to the single-node stack, health-gated, with automatic rollback
# (ADR 0021; the same contract as deploy/scripts/rollout.sh for Kubernetes).
#
#   deploy.sh <registry/repo@sha256:...> [timeout seconds, default 300]
#
# 1. Only a digest is accepted (a tag can move). Unless VERIFY_SIGNATURE=0, the image must
#    carry the keyless signature of this repository's CI on main (cosign, run from its
#    pinned image: nothing to install).
# 2. Migrations run once (`compose run migrate`), then `docker compose up --wait`: the
#    API must turn healthy (/readyz: schema, catalog, Postgres, Qdrant, Redis).
# 3. Smoke test: /readyz and an authenticated /v1/agents with the first API key.
# Any failure puts the previous image back, waits for it to be healthy, and exits 1.
# The running image is recorded as API_IMAGE in .env; earlier ones in .deploy-history.
set -euo pipefail
cd "$(dirname "$0")"

image="${1:?usage: deploy.sh <registry/repo@sha256:...> [timeout seconds]}"
timeout="${2:-300}"
COSIGN_IMAGE="ghcr.io/sigstore/cosign/cosign:v3.1.3@sha256:9e5c2f2edc34351160407ca3416c61855bdf9403c3c5936e0f0be7fc261611b8"
# Signed by CI on main of this repository; set CI_IDENTITY for a fork.
CI_IDENTITY="${CI_IDENTITY:-^https://github.com/tatymarisol44-cmyk/Tatos/.github/workflows/ci.yml@refs/heads/main$}"

case "$image" in
  *@sha256:????????????????????????????????????????????????????????????????) ;;
  *) echo "deploy.sh: '$image' is not pinned by digest (repo@sha256:<64 hex>)" >&2; exit 2 ;;
esac
[ -f .env ] || { echo "deploy.sh: no .env here; run install.sh first" >&2; exit 2; }

env_get() { sed -n "s/^$1=//p" .env | tail -1; }
env_set() {  # replace or append KEY=value in .env, keeping the file private
  if grep -q "^$1=" .env; then sed -i "s|^$1=.*|$1=$2|" .env; else echo "$1=$2" >> .env; fi
  chmod 600 .env
}

if [[ ",$(env_get COMPOSE_PROFILES)," == *",tunnel,"* ]] && [ -z "$(env_get TUNNEL_TOKEN)" ]; then
  echo "deploy.sh: COMPOSE_PROFILES has 'tunnel' but TUNNEL_TOKEN is empty" >&2; exit 2
fi

verify() {
  [ "${VERIFY_SIGNATURE:-1}" = "0" ] && { echo "signature check skipped (VERIFY_SIGNATURE=0)"; return 0; }
  docker run --rm "$COSIGN_IMAGE" verify "$1" \
    --certificate-identity-regexp "$CI_IDENTITY" \
    --certificate-oidc-issuer https://token.actions.githubusercontent.com > /dev/null
}

smoke() {
  local key
  key="$(env_get API_KEYS | cut -d, -f1 | cut -d: -f1)"
  curl -fsS --max-time 5 http://127.0.0.1:8000/readyz > /dev/null &&
    curl -fsS --max-time 10 -H "X-API-Key: $key" http://127.0.0.1:8000/v1/agents > /dev/null
}

annotate() {  # <title> <text>: under GitHub Actions, a failure anyone can read (no login)
  [ "${GITHUB_ACTIONS:-}" = "true" ] || return 0
  local text="${2//'%'/'%25'}"
  text="${text//$'\r'/'%0D'}"
  echo "::error title=$1::${text//$'\n'/'%0A'}"
}

migrate() {  # the schema, once, before the API (its output is lost with --rm: keep it)
  local out rc=0
  out="$(docker compose --profile migrate run --rm -T migrate 2>&1)" || rc=$?
  echo "$out"
  [ "$rc" = 0 ] || annotate "single-node migrate" "$(tail -n 40 <<< "$out")"
  return "$rc"
}

start() {
  docker compose up -d --remove-orphans --wait --wait-timeout "$timeout"
}

up() {  # <image>
  env_set API_IMAGE "$1"
  migrate && start
}

previous="$(env_get API_IMAGE)"
echo "current:   ${previous:-<none>}"
echo "deploying: $image"

rollback() {
  echo "::error::deploy of $image failed${1:+: $1}" >&2
  docker compose ps -a >&2 || true
  for svc in api postgres qdrant redis; do
    annotate "single-node $svc" "$(docker compose logs --no-color --tail 40 "$svc" 2>&1 || true)"
  done
  if [ -n "$previous" ] && [ "$previous" != "$image" ]; then
    echo "rolling back to $previous" >&2
    up "$previous" && smoke || echo "::error::still unhealthy after the rollback" >&2
  else
    env_set API_IMAGE "$previous"
  fi
  exit 1
}

verify "$image" || rollback "signature not valid for $CI_IDENTITY"
API_IMAGE="$image" docker compose pull --quiet api || rollback "image not found"
env_set API_IMAGE "$image"
migrate || rollback "schema migration failed"
start || rollback "not healthy within ${timeout}s"
smoke || rollback "smoke test failed"
echo "$(date -u +%FT%TZ) $image" >> .deploy-history
echo "deployed $image"
