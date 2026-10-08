# Service level objectives

Audit 2026-10-08, observability. This file holds the objectives, the signals behind each
one, and the alert that fires when one is at risk. Every alert in
`deploy/monitoring/alerts.yml` links to a runbook in `docs/runbooks/`.

## Four separate kinds of record

| Kind | Where | Holds | Never holds |
|---|---|---|---|
| Logs | stdout → the cluster's log store | technical events, error types | personal data (redacted when the record is created, `telemetry.redact_record`) |
| Metrics | `/metrics` on port 9464 (pod-internal) → Prometheus / Google Managed Prometheus | counts, durations, backlogs | ids of patients, threads or documents (labels are route **templates**) |
| Traces | OTLP → collector (optional) | span timings, model names, token counts | prompt or answer text (`attributes/scrub` in the collector) |
| Audit | `audit_events`, hash-chained, anchored hourly | who did what to which record | message text |

The technical log is not the clinical audit trail: deleting logs never touches the audit.

## Objectives

"Administrative API" means every route except the ones that call a model:
`/v1/chat`, `/v1/chat/stream`, `/v1/me/chat`, `/a2a`, `/v1/insights/ask`, `/v1/route`,
and the probes. A model outage must not count against it (the circuit breaker answers
those routes with 503).

| # | Objective | Target | Window | Signal |
|---|---|---|---|---|
| SLO-1 | Administrative API availability: answers that are not 5xx | 99.9 % | 30 days | `agency_http_server_duration_seconds_count` by `status_class` |
| SLO-2 | Administrative API latency: requests served in under 300 ms | 95 % | 30 days | `agency_http_server_duration_seconds_bucket{le="0.3"}` |
| SLO-3 | A crisis alert is taken (acknowledged) by a person | within 15 min, always | each alert | `agency_alerts_oldest_open_age_seconds` |
| SLO-4 | Answers held for review are decided | backlog under 20 for more than 1 h | continuous | `agency_reviews_pending` |
| SLO-5 | Cross-tenant data exposure | 0 | always | not a metric: `tests/test_tenant_isolation_matrix.py` gates every release |
| SLO-6 | Sensitive clinical actions audited | 100 % | always | `agency audit-verify --anchors` (CronJob) and the regression tests |

The error budget of SLO-1 is 0.1 % of requests: about 43 minutes of full outage in 30 days.

## Alerting: burn rates

SLO-1 uses multi-window burn-rate alerts (Google SRE workbook, chapter 5). A burn rate of
1 spends the budget in exactly 30 days.

| Alert | Burn rate | Long window | Short window | Budget spent when it fires | Severity |
|---|---|---|---|---|---|
| `ApiErrorBudgetFastBurn` | 14.4 | 1 h | 5 min | 2 % | page |
| `ApiErrorBudgetSlowBurn` | 6 | 6 h | 30 min | 5 % | page |
| `ApiErrorBudgetTicket` | 1 | 3 d | 6 h | 10 % | ticket |

The short window stops the alert as soon as the problem ends.

## Other signals (dashboards, not objectives)

- Model: `agency_llm_calls_total` by outcome (ok, error, circuit_open),
  `agency_llm_duration_seconds`, `gen_ai_client_token_usage_total`, and cost per request
  in the usage ledger (`usage.py`).
- Breakers: `agency_circuit_opened_total` by dependency.
- Streaming: `agency_sse_connections` (open SSE responses per pod).
- Outbox: `agency_outbox_pending` (campaign messages not yet sent).
- Rate limiting: `agency_http_server_duration_seconds_count{status_class="4xx"}` (429
  shows up here; see the rate-limit runbook).
- Kubernetes (kube-state-metrics): available replicas, restarts, CronJob success.

## Not measured yet

- Pool saturation of Postgres, Redis latency and Qdrant latency: they need the database
  and Redis exporters of the managed services (Cloud SQL and Memorystore expose them in
  Cloud Monitoring). This is decided when production infrastructure is chosen.
- Real-user latency from the browser: there is no front-end telemetry, on purpose
  (patient privacy). Add it only with consent and without identifiers.
