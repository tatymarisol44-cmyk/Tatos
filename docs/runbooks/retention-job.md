# Retention job

1. **Check the CronJob and its last run.** Is `kubectl -n agency get cronjob agency-retention`
   suspended? Then read `kubectl -n agency logs job/<last job>`.
2. **Run it by hand:** `kubectl -n agency create job --from=cronjob/agency-retention retention-manual`.
3. **What it deletes:** only what the retention rule allows, meaning conversations older than
   `THREAD_RETENTION_DAYS`, expired memory and abandoned document versions. It never deletes
   the clinical record.
