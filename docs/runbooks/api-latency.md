# Administrative API latency (SLO-2)

**What it means:** more than 5 % of non-model requests have taken over 300 ms for 15 minutes.

1. **Which routes are slow?** Run this in Prometheus:
   `histogram_quantile(0.95, sum by (le, route) (rate(agency_http_server_duration_seconds_bucket[10m])))`
2. **Are the pods CPU-bound?** Run `kubectl -n agency top pods`. The HPA scales on CPU between
   2 and 10 replicas. If it is already at 10, raise `maxReplicas` in
   `deploy/k8s/base/hpa.yaml` or grow the node pool.
3. **Is it the database?** Cloud SQL Query Insights lists the slowest statements. Every
   session has a 15 s `statement_timeout`, so a runaway query fails instead of holding the pool.
4. **Compare** with the load-test baseline in `docs/load-test.md`.
