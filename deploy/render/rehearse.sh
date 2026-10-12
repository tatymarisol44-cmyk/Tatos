#!/usr/bin/env bash
# Rehearse the free Render profile (ADR 0022) before it reaches production.
#
#   rehearse.sh <registry/repo@sha256:...>      (needs Docker, yq v4, openssl, curl, jq)
#
# 1. The API image runs with the free instance's limits (512 MB, no swap, 0.1 CPU) and the
#    environment render.yaml declares: its fixed values, random secrets for the generated
#    ones, and a value for every one the owner types in the dashboard.
# 2. The managed services are stood in for as production reaches them: Postgres over TLS
#    (Supabase), Redis over verified TLS with a password (Upstash), Qdrant with an API key
#    (Qdrant Cloud). Their images are the pinned ones of deploy/single-node/compose.yaml.
# 3. Every demo flow runs from another container, so it neither shares the 0.1 CPU nor
#    counts in the API's memory.
# 4. Memory budget: the API's peak (cgroup memory.peak) plus what loading the real model
#    client (LiteLLM, not loaded with LLM_BACKEND=fake) adds must stay under BUDGET_MB.
set -euo pipefail

image="${1:?usage: rehearse.sh <registry/repo@sha256:...>}"
here="$(cd "$(dirname "$0")" && pwd)"
blueprint="$here/render.yaml"
compose="$here/../single-node/compose.yaml"
BUDGET_MB="${BUDGET_MB:-460}" # of 512: room for the allocator and request bursts
READY_TIMEOUT="${READY_TIMEOUT:-600}"
net=agency-render
work="$(mktemp -d)"

say() { printf '\n== %s\n' "$*"; }
rand() { openssl rand -hex "$1"; }
img() { yq ".services.$1.image" "$compose"; }
annotate() { # <title> <text>: under GitHub Actions, a failure anyone can read (no login)
  [ "${GITHUB_ACTIONS:-}" = "true" ] || { printf '%s\n%s\n' "$1" "$2" >&2; return 0; }
  local text="${2//'%'/'%25'}"
  text="${text//$'\r'/'%0D'}"
  echo "::error title=$1::${text//$'\n'/'%0A'}"
}
on_exit() {
  local code=$?
  if [ "$code" -ne 0 ]; then
    for c in api postgres redis qdrant; do
      annotate "render rehearsal $c" "$(docker logs --tail 40 "$c" 2>&1 || true)"
    done
    annotate "render rehearsal api state" \
      "$(docker inspect -f 'oom={{.State.OOMKilled}} exit={{.State.ExitCode}}' api 2>&1 || true)"
  fi
  docker rm -f api postgres redis qdrant > /dev/null 2>&1 || true
  docker network rm "$net" > /dev/null 2>&1 || true
  rm -rf "$work"
  exit "$code"
}
trap on_exit EXIT

pg_pw="$(rand 16)"
redis_pw="$(rand 16)"
qdrant_key="$(rand 16)"
key="svc_$(rand 24)"
tenant=rehearsal

say "stand-ins for Supabase, Upstash and Qdrant Cloud"
docker network create "$net" > /dev/null
openssl req -x509 -newkey rsa:2048 -nodes -days 2 -subj "/CN=agency-render-rehearsal" \
  -addext "subjectAltName=DNS:postgres,DNS:redis" \
  -keyout "$work/tls.key" -out "$work/tls.crt" 2> /dev/null
chmod 644 "$work/tls.crt"
# Each server gets its own copy of the key, owned by its user (the images' entrypoints
# drop from root to that user).
docker run -d --name postgres --network "$net" -v "$work:/tls-src:ro" \
  -e POSTGRES_USER=postgres -e POSTGRES_PASSWORD="$pg_pw" -e POSTGRES_DB=postgres \
  --entrypoint sh "$(img postgres)" -c '
    cp /tls-src/tls.crt /tls-src/tls.key /tmp/ && chown postgres:postgres /tmp/tls.* &&
    chmod 600 /tmp/tls.key &&
    exec docker-entrypoint.sh postgres -c ssl=on -c ssl_cert_file=/tmp/tls.crt \
      -c ssl_key_file=/tmp/tls.key' > /dev/null
docker run -d --name redis --network "$net" -v "$work:/tls-src:ro" \
  --entrypoint sh "$(img redis)" -c "
    cp /tls-src/tls.crt /tls-src/tls.key /tmp/ && chown redis:redis /tmp/tls.* &&
    chmod 600 /tmp/tls.key &&
    exec docker-entrypoint.sh redis-server --port 0 --tls-port 6379 \
      --tls-cert-file /tmp/tls.crt --tls-key-file /tmp/tls.key --tls-auth-clients no \
      --requirepass $redis_pw --save '' --appendonly no" > /dev/null
docker run -d --name qdrant --network "$net" -e QDRANT__SERVICE__API_KEY="$qdrant_key" \
  -e QDRANT__TELEMETRY_DISABLED=true "$(img qdrant)" > /dev/null
for _ in $(seq 60); do
  docker exec postgres pg_isready -U postgres -q 2> /dev/null &&
    docker exec redis redis-cli --tls --cacert /tmp/tls.crt -a "$redis_pw" --no-auth-warning ping \
      2> /dev/null | grep -q PONG && break
  sleep 2
done
docker exec postgres pg_isready -U postgres -q

say "the API's environment, from render.yaml"
{
  yq '.services[0].envVars[] | select(has("value")) | .key + "=" + .value' "$blueprint"
  for k in $(yq '.services[0].envVars[] | select(.generateValue == true) | .key' "$blueprint"); do
    echo "$k=$(rand 32)"
  done
  # What the owner types in the dashboard (sync: false), here for the stand-ins.
  cat << EOF
DATABASE_URL=postgresql+psycopg://postgres:$pg_pw@postgres:5432/postgres?sslmode=require
POSTGRES_URL=postgresql://postgres:$pg_pw@postgres:5432/postgres?sslmode=require
REDIS_URL=rediss://default:$redis_pw@redis:6379
QDRANT_URL=http://qdrant:6333
QDRANT_API_KEY=$qdrant_key
API_KEYS=$key:$tenant
TENANT_PACKS={"$tenant":"ec-psychologist"}
PRACTICE_DISPLAY_NAME=Consultorio de ensayo
PUBLIC_BASE_URL=https://agency-api.onrender.com
LLM_BACKEND=fake
EOF
} > "$work/api.env"
for k in $(yq '.services[0].envVars[] | select(.sync == false) | .key' "$blueprint"); do
  grep -q "^$k=" "$work/api.env" || {
    echo "rehearse.sh: render.yaml asks the owner for $k; give the rehearsal a value" >&2
    exit 2
  }
done
meta="$(sed -n 's/^META_APP_SECRET=//p' "$work/api.env")"

say "the API under the free instance's limits: 512 MB, no swap, 0.1 CPU"
# No Docker health check: Render does not run it. SSL_CERT_FILE: the stand-in Redis
# certificate is the only trusted authority, so the rediss:// connection is verified as it
# is against Upstash's public certificate.
docker run -d --name api --network "$net" -p 127.0.0.1:8000:8000 \
  --memory 512m --memory-swap 512m --cpus 0.1 --no-healthcheck \
  --env-file "$work/api.env" -e SSL_CERT_FILE=/tls/ca.crt \
  -v "$work/tls.crt:/tls/ca.crt:ro" "$image" > /dev/null
start=$SECONDS
until curl -fsS --max-time 5 http://127.0.0.1:8000/readyz > /dev/null 2>&1; do
  [ "$(docker inspect -f '{{.State.Running}}' api)" = true ] || {
    echo "rehearse.sh: the API stopped before it was ready" >&2
    exit 1
  }
  [ $((SECONDS - start)) -lt "$READY_TIMEOUT" ] || {
    echo "rehearse.sh: not ready after ${READY_TIMEOUT}s" >&2
    exit 1
  }
  sleep 5
done
ready_s=$((SECONDS - start))
echo "ready after ${ready_s}s (migrations included)"

say "every demo flow, from its own container"
docker run --rm --network "$net" "$image" \
  agency demo --url http://api:8000 --key "$key" --meta-app-secret "$meta"
curl -fsS --max-time 10 http://127.0.0.1:8000/readyz > /dev/null

say "memory budget"
state="$(docker inspect -f '{{.State.Running}} {{.State.OOMKilled}} {{.RestartCount}}' api)"
[ "$state" = "true false 0" ] || { echo "rehearse.sh: API state '$state'" >&2; exit 1; }
peak="$(docker exec api cat /sys/fs/cgroup/memory.peak 2> /dev/null ||
  cat "/sys/fs/cgroup/system.slice/docker-$(docker inspect -f '{{.Id}}' api).scope/memory.peak")"
peak_mb=$((peak / 1048576))
# Measured in a separate container: loading LiteLLM inside the API's would count twice.
litellm_mb="$(docker run --rm "$image" python -c '
import re

def rss():
    status = open("/proc/self/status").read()
    return int(re.search(r"VmRSS:\s+(\d+)", status).group(1)) // 1024

import orchestrator.api.app
before = rss()
import litellm
print(rss() - before)')"
total=$((peak_mb + litellm_mb))
# The peak counts the page cache too, which the kernel reclaims before killing anything:
# the split now says how much of the budget is the process's own memory.
stat_mb() { docker exec api awk -v k="$1" '$1 == k { print int($2 / 1048576) }' /sys/fs/cgroup/memory.stat 2> /dev/null || echo "?"; }
summary="API peak ${peak_mb} MB + LiteLLM ${litellm_mb} MB = ${total} MB (budget ${BUDGET_MB} of 512 MB); after the demo: anon $(stat_mb anon) MB, file cache $(stat_mb file) MB; ready after ${ready_s}s at 0.1 CPU"
echo "$summary"
[ -z "${GITHUB_STEP_SUMMARY:-}" ] || echo "Render free profile: $summary" >> "$GITHUB_STEP_SUMMARY"
[ "$total" -le "$BUDGET_MB" ] || {
  annotate "render memory budget" "$summary"
  exit 1
}
echo "rehearsal passed"
