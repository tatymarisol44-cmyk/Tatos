#!/usr/bin/env bash
# Nightly backup of the single-node stack (ADR 0021), run by the agency-backup.timer that
# install.sh creates. Same artefacts as deploy/backup/backup.sh:
#   backups/agency-<UTC>.dump            pg_dump custom format (business data + checkpoints)
#   backups/agency-<UTC>.dump.sha256     its checksum
#   backups/agency-<UTC>.dump.anchors.jsonl  audit-chain anchors, to verify a restore
# Keeps BACKUP_KEEP_DAYS (14) locally. With BACKUP_REMOTE set in .env (an rclone remote
# such as ":s3,provider=Cloudflare,env_auth=true,endpoint=https://<id>.r2.cloudflarestorage.com:bucket/agency"
# plus AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY), each backup is also copied off the
# server: a backup on the same disk does not survive the loss of the server.
# Qdrant is not backed up: `agency knowledge reindex` rebuilds it from Postgres.
set -euo pipefail
cd "$(dirname "$0")"
RCLONE_IMAGE="rclone/rclone:1.75.2@sha256:2687085f718d3c628f7fdfb77c52a1d344332aed543110bb88088f2c70d43eb5"

env_get() { sed -n "s/^$1=//p" .env | tail -1; }
dir="$PWD/backups"
mkdir -p "$dir"
chmod 700 "$dir"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
out="$dir/agency-$stamp.dump"

# Anchors first: the dump then contains them, and a restore must reproduce them.
docker compose exec -T api agency audit-anchor > "$out.anchors.jsonl"
docker compose exec -T postgres pg_dump -U agency -d agency \
  --format=custom --no-owner --no-privileges > "$out"
docker compose exec -T postgres pg_restore --list < "$out" > /dev/null  # readable dump
(cd "$dir" && sha256sum "$(basename "$out")" > "$(basename "$out").sha256")
chmod 600 "$out" "$out".*

keep="$(env_get BACKUP_KEEP_DAYS)"
find "$dir" -name 'agency-*' -mtime +"${keep:-14}" -delete

remote="$(env_get BACKUP_REMOTE)"
if [ -n "$remote" ]; then
  # Only the two storage credentials reach the rclone container, not the rest of .env.
  docker run --rm \
    -e AWS_ACCESS_KEY_ID="$(env_get AWS_ACCESS_KEY_ID)" \
    -e AWS_SECRET_ACCESS_KEY="$(env_get AWS_SECRET_ACCESS_KEY)" \
    -v "$dir:/backups:ro" "$RCLONE_IMAGE" \
    copy /backups "$remote" --include "agency-$stamp.*"
  echo "copied off the server: $remote"
fi
echo "$out"
