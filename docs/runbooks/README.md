# Runbooks

One page per alert in `deploy/monitoring/alerts.yml`. Each alert's `runbook` annotation
points here, and `tests/test_observability.py` fails if a page is missing. The objectives
are in [`docs/slo.md`](../slo.md).

## Who is paged (decision O1)

This is the on-call for the PLATFORM: infrastructure, the API, the pipelines. It is not the
clinics' crisis on-call, which reaches each practice's own professionals (ADR 0017).

- **Who.** The platform operator, until a second engineer joins. Then a weekly rotation
  and a paging service with escalation.
- **`severity=page`.** A Telegram message to the operators' group, plus e-mail when an SMTP
  provider is configured. It repeats every hour until resolved. Act now: users are affected
  or soon will be.
- **`severity=ticket`.** An e-mail, repeated daily while firing (on Telegram when there is no
  e-mail). Handle it in working hours.
- **Configuration.** `deploy/monitoring/render_alertmanager.py` renders the Alertmanager
  config of Google Managed Prometheus from environment variables. The credentials are
  never in git.
- **Privacy.** Alerts carry only rule labels (alert name, SLO), never patient data.

| Page | Alerts |
|---|---|
| [incident.md](incident.md) | how to run any incident; severities; who decides |
| [data-breach.md](data-breach.md) | suspected exposure of personal or health data |
| [break-glass.md](break-glass.md) | emergency access when single sign-on is unavailable |
| [api-errors.md](api-errors.md) | ApiErrorBudgetFastBurn, ApiErrorBudgetSlowBurn, ApiErrorBudgetTicket |
| [api-latency.md](api-latency.md) | ApiLatencySloAtRisk |
| [crisis-alert-unattended.md](crisis-alert-unattended.md) | CrisisAlertUnattended |
| [review-backlog.md](review-backlog.md) | ReviewBacklogHigh |
| [privacy-deadline.md](privacy-deadline.md) | PrivacyDeadlineAtRisk |
| [model-outage.md](model-outage.md) | ModelCircuitOpen, ModelErrorRateHigh |
| [outbox-stuck.md](outbox-stuck.md) | OutboxStuck |
| [capacity.md](capacity.md) | ModelRoutesBusy |
| [no-ready-replicas.md](no-ready-replicas.md) | ApiNoReadyReplicas, ApiPodsRestarting |
| [retention-job.md](retention-job.md) | RetentionJobFailed, RetentionJobNotRunning |
| [restore.md](restore.md) | restoring the database from a backup (RPO/RTO) |

The commands assume namespace `agency` and the `gke` overlay.
