# Runbooks

One page per alert in `deploy/monitoring/alerts.yml`. Each alert's `runbook` annotation
points here, and `tests/test_observability.py` fails if a page is missing. The objectives
are in [`docs/slo.md`](../slo.md).

| Page | Alerts |
|---|---|
| [incident.md](incident.md) | how to run any incident; severities; who decides |
| [data-breach.md](data-breach.md) | suspected exposure of personal or health data |
| [break-glass.md](break-glass.md) | emergency access when single sign-on is unavailable |
| [api-errors.md](api-errors.md) | ApiErrorBudgetFastBurn, ApiErrorBudgetSlowBurn, ApiErrorBudgetTicket |
| [api-latency.md](api-latency.md) | ApiLatencySloAtRisk |
| [crisis-alert-unattended.md](crisis-alert-unattended.md) | CrisisAlertUnattended |
| [review-backlog.md](review-backlog.md) | ReviewBacklogHigh |
| [model-outage.md](model-outage.md) | ModelCircuitOpen, ModelErrorRateHigh |
| [outbox-stuck.md](outbox-stuck.md) | OutboxStuck |
| [capacity.md](capacity.md) | ModelRoutesBusy |
| [no-ready-replicas.md](no-ready-replicas.md) | ApiNoReadyReplicas, ApiPodsRestarting |
| [retention-job.md](retention-job.md) | RetentionJobFailed, RetentionJobNotRunning |
| [restore.md](restore.md) | restoring the database from a backup (RPO/RTO) |

The commands assume namespace `agency` and the `gke` overlay.
