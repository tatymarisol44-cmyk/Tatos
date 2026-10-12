#!/usr/bin/env bash
# Deploy one API image to the Render service (ADR 0022) and wait until Render reports it
# live. Render starts the new instance next to the running one, gates it on /readyz and
# only then moves the traffic: a release that does not start never serves, and the running
# one keeps serving (the same contract as deploy/single-node/deploy.sh).
#
#   RENDER_API_KEY=... RENDER_SERVICE_ID=srv-... [PUBLIC_URL=https://...] \
#     deploy.sh <registry/repo@sha256:...> [timeout seconds, default 900]
#
# 1. Only a digest is accepted (a tag can move). Unless VERIFY_SIGNATURE=0, the image must
#    carry the keyless signature of this repository's CI on main (cosign, from its pinned
#    image, with the identity deploy/single-node/deploy.sh checks).
# 2. Render's API deploys that digest; the deploy is followed until `live`, or fails.
# 3. With PUBLIC_URL, /readyz must answer through Render's public entrance.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"

image="${1:?usage: deploy.sh <registry/repo@sha256:...> [timeout seconds]}"
timeout="${2:-900}"
POLL_SECONDS="${POLL_SECONDS:-10}"
case "$image" in
  *@sha256:????????????????????????????????????????????????????????????????) ;;
  *) echo "deploy.sh: '$image' is not pinned by digest (repo@sha256:<64 hex>)" >&2; exit 2 ;;
esac
: "${RENDER_API_KEY:?deploy.sh: set RENDER_API_KEY}"
: "${RENDER_SERVICE_ID:?deploy.sh: set RENDER_SERVICE_ID (srv-...)}"

annotate() { # <title> <text>: under GitHub Actions, a failure anyone can read (no login)
  [ "${GITHUB_ACTIONS:-}" = "true" ] || { printf '%s: %s\n' "$1" "$2" >&2; return 0; }
  echo "::error title=$1::$2"
}

# One source for the signature rule: the single-node script's pinned cosign and identity.
single_node="$here/../single-node/deploy.sh"
COSIGN_IMAGE="$(sed -n 's/^COSIGN_IMAGE="\(.*\)"$/\1/p' "$single_node")"
CI_IDENTITY="${CI_IDENTITY:-$(sed -n 's/^CI_IDENTITY="\${CI_IDENTITY:-\(.*\)}"$/\1/p' "$single_node")}"
[ -n "$COSIGN_IMAGE" ] && [ -n "$CI_IDENTITY" ] || {
  echo "deploy.sh: cosign image or CI identity not found in $single_node" >&2
  exit 2
}
if [ "${VERIFY_SIGNATURE:-1}" = "0" ]; then
  echo "signature check skipped (VERIFY_SIGNATURE=0)"
elif ! docker run --rm "$COSIGN_IMAGE" verify "$image" \
  --certificate-identity-regexp "$CI_IDENTITY" \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com > /dev/null; then
  annotate "render deploy" "$image is not signed by this repository's CI on main"
  exit 1
fi

api="https://api.render.com/v1/services/$RENDER_SERVICE_ID/deploys"
render() { curl -fsS --max-time 30 -H "Authorization: Bearer $RENDER_API_KEY" \
  -H "Accept: application/json" "$@"; }

deploy_id="$(render -X POST -H "Content-Type: application/json" \
  -d "$(jq -nc --arg url "$image" '{imageUrl: $url}')" "$api" | jq -r '.id // empty')" ||
  deploy_id=""
[ -n "$deploy_id" ] || { annotate "render deploy" "Render did not accept $image"; exit 1; }
echo "render deploy $deploy_id: $image"

deadline=$((SECONDS + timeout))
while :; do
  status="$(render "$api/$deploy_id" | jq -r '.status // "unknown"')" || status=unreachable
  case "$status" in
    live) break ;;
    *_failed | canceled | deactivated)
      annotate "render deploy" "$deploy_id ended '$status'; the previous release keeps serving"
      exit 1
      ;;
  esac
  if [ "$SECONDS" -ge "$deadline" ]; then
    annotate "render deploy" "$deploy_id still '$status' after ${timeout}s"
    exit 1
  fi
  sleep "$POLL_SECONDS"
done
echo "live"

if [ -n "${PUBLIC_URL:-}" ]; then
  curl -fsS --max-time 30 --retry 5 --retry-delay 10 --retry-all-errors \
    "${PUBLIC_URL%/}/readyz" > /dev/null || {
    annotate "render deploy" "live, but ${PUBLIC_URL%/}/readyz does not answer"
    exit 1
  }
  echo "serving at $PUBLIC_URL"
fi
