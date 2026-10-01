#!/usr/bin/env bash
# Back up the business database (audit, reviews, consents, CRM, campaigns, keys,
# knowledge pointers) and the LangGraph checkpoints, which share it in Compose.
#   PGURL=postgresql://user:pass@host:5432/agency?sslmode=require ./backup.sh /backups
# Writes <dir>/agency-<UTC timestamp>.dump (pg_dump custom format), its SHA-256, and the
# audit-chain anchors taken at the same moment, to verify a restore against. Copy all
# three to storage outside the cluster (with object lock / versioning).
# The pg_dump major version must be >= the server's.
set -euo pipefail
dir="${1:?usage: backup.sh <output dir>}"
: "${PGURL:?set PGURL to the database to back up}"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
out="$dir/agency-$stamp.dump"
mkdir -p "$dir"
agency audit-anchor > "$out.anchors.jsonl"   # before the dump: the dump contains them
pg_dump --format=custom --no-owner --no-privileges --file="$out" "$PGURL"
sha256sum "$out" > "$out.sha256"
echo "$out"
