#!/usr/bin/env bash
# Why a deploy is not Ready, as GitHub annotations (readable on the run page without
# signing in; the raw job log is not). Used by deploy.yml when the rehearsal fails.
#
#   diagnose.sh <namespace>
#
# Prints: pods that are not Ready with each container's state, Warning events, and the
# last lines of every container that is waiting, crashed or not ready. The rehearsal runs
# synthetic data and a throwaway key only; the app's logs redact personal data anyway.
set -uo pipefail
ns="${1:?usage: diagnose.sh <namespace>}"
lines="${DIAGNOSE_LOG_LINES:-30}"

esc() { sed -e 's/%/%25/g' -e 's/\r/%0D/g' | awk 'BEGIN{ORS="%0A"} {print}'; }

kubectl -n "$ns" get pods -o wide || true

kubectl -n "$ns" get pods -o json | python3 "$(dirname "$0")/pod_problems.py" | while IFS=$'\t' read -r pod container summary; do
  logs=$( { kubectl -n "$ns" logs "$pod" -c "$container" --tail="$lines" 2>&1;
            kubectl -n "$ns" logs "$pod" -c "$container" --previous --tail="$lines" 2>/dev/null; } | tail -n "$lines")
  printf '::error title=%s/%s not ready::%s%%0A%s\n' "$pod" "$container" \
    "$(printf '%s' "$summary" | esc)" "$(printf '%s' "$logs" | esc)"
done

kubectl -n "$ns" get events --field-selector type=Warning \
  -o custom-columns=OBJ:.involvedObject.name,REASON:.reason,MSG:.message --no-headers 2>/dev/null \
  | tail -n 20 | while read -r line; do
      printf '::warning title=event::%s\n' "$(printf '%s' "$line" | esc)"
    done
exit 0
