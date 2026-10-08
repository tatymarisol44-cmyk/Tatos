# Restoring from a backup

## Objectives (proposed; owner decision O2)

| | Target | How it is met |
|---|---|---|
| RPO, ordinary failure (bad deploy, deleted rows, DB instance lost) | ≤ 5 min | Cloud SQL automated backups + point-in-time recovery (write-ahead log kept 7 days) |
| RPO, disaster (project or region lost, ransomware) | ≤ 24 h | daily `deploy/backup/backup.sh` dump to a bucket in another region with a retention lock |
| RTO | ≤ 1 h | restore into a new instance, verify, switch `DATABASE_URL` |

A backup counts only if a restore of it has been verified: `tests/test_backup_restore.py`
runs the whole cycle in CI on every commit, and an operator rehearses it on a copy of
production every quarter (record the date and duration below).

## Procedure

1. **Freeze writes:** `kubectl -n agency scale deployment/agency-orchestrator --replicas=0`
   and suspend the CronJobs (`kubectl -n agency patch cronjob <name> -p '{"spec":{"suspend":true}}'`).
2. **Restore into an empty database**, never over the live one:
   - Point in time: Cloud SQL console → *Clone* → *Clone to a point in time* (just before
     the incident).
   - From a dump: `TARGET=postgresql://...agency_restore ./deploy/backup/restore.sh agency-<stamp>.dump`.
     The script checks the SHA-256, restores, checks the schema revision and verifies the
     audit chain against the anchors taken with the backup. It refuses on any mismatch.
3. **Rebuild the vector index** from the restored database: `agency knowledge reindex`.
   The database holds the canonical text of every knowledge document (migration 0009), so
   Qdrant needs no backup of its own. Documents uploaded before 0009 are listed as
   `missing_text` and must be uploaded again. The preference memory (closed vocabulary,
   also in Qdrant) is not rebuilt: it is re-learned from new conversations, by design
   the least valuable data the system keeps.
4. **Switch** `DATABASE_URL`/`POSTGRES_URL` in the Secret to the restored instance, scale the
   API back up, resume the CronJobs.
5. **Verify** `agency audit-verify` for every tenant and spot-check one clinic with its owner.

## Rehearsals

| Date | Source | Duration | Result | By |
|---|---|---|---|---|
| — | — | — | not yet run on production infrastructure | — |
