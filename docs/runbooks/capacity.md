# Capacity: assistant requests refused (503 busy)

**What it means:** pods reached `MAX_INFLIGHT_MODEL_REQUESTS` (32 per pod by default) and
answered the next assistant requests with 503 and `Retry-After`. Agenda, CRM and records
are never limited by this.

1. **Is the HPA scaling?** Run `kubectl -n agency get hpa agency-orchestrator`.
   - If it is at `maxReplicas`, raise it in `deploy/k8s/base/hpa.yaml`, and the node pool
     (Autopilot) budget.
   - If it is not scaling: CPU stays low while requests wait on the model. Lower the
     per-pod limit, so memory and CPU reflect the load, or add replicas by hand
     (`kubectl -n agency scale deploy/agency-orchestrator --replicas=N`). The HPA takes
     over again afterwards.
2. **Is the provider slower than usual?** Look at `agency_llm_duration_seconds` p95.
   Slower answers hold slots longer: the same traffic needs more of them.
3. **Is the provider rate-limiting us?** 429 errors in `agency_llm_calls_total{outcome="error"}`.
   Raise the quota with the provider; more pods will not help.
4. **Do not raise the per-pod limit blindly.** Every in-flight conversation holds memory
   and, while it writes, a database connection (pool 10 + overflow 5 per pod).
