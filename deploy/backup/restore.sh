#!/usr/bin/env bash
# Restore a backup into an EMPTY database and prove it is whole before using it:
#   TARGET=postgresql://user:pass@host:5432/agency_restore ./restore.sh agency-<stamp>.dump
# 1. checks the SHA-256 written by backup.sh;
# 2. pg_restore (exits non-zero on any error);
# 3. the schema must be at the revision this release expects (`agency db check`);
# 4. every tenant's audit chain must verify AND still contain the anchors taken when
#    the backup was made (`agency audit-verify --anchors`).
# Point the service at TARGET only after this script succeeds.
set -euo pipefail
dump="${1:?usage: restore.sh <file.dump>}"
: "${TARGET:?set TARGET to an empty database}"
sha256sum --check "$dump.sha256"
pg_restore --exit-on-error --no-owner --no-privileges --dbname="$TARGET" "$dump"
export DATABASE_URL="${TARGET/postgresql:\/\//postgresql+psycopg://}"
agency db check
agency audit-verify --anchors "$dump.anchors.jsonl"
echo "restore verified: $TARGET"
