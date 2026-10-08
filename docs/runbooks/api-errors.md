# Administrative API errors (SLO-1)

**What it means:** more than the budgeted 0.1 % of non-model requests answer 5xx.

1. **Which routes fail?** Run this in Prometheus:
   `sum by (route) (rate(agency_http_server_duration_seconds_count{status_class="5xx"}[10m]))`
2. **Did it start with a deploy?** Check `kubectl -n agency rollout history deployment/agency-orchestrator`.
   If so, run `kubectl -n agency rollout undo deployment/agency-orchestrator` and investigate after.
3. **Is it the database?**
   - Run `kubectl -n agency get pods`. If pods are not ready, go to [no-ready-replicas.md](no-ready-replicas.md).
   - Read the logs: `kubectl -n agency logs deploy/agency-orchestrator --since=15m | grep -E "ERROR|Traceback"`.
   - A `QueryCanceled` or `statement timeout` error means a slow query: go to [api-latency.md](api-latency.md).
4. **Redis is not the cause.** A Redis outage only degrades rate limiting to per-pod buckets
   (`RATE_LIMIT_ON_OUTAGE=local`); it does not cause 5xx.
