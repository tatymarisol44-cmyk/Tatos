# Model provider outage

**What it means:** model calls are failing. After 5 failures in a row, the circuit breaker
opens for 30 s and the assistant answers 503 with `Retry-After`. Agenda, CRM, records and
campaigns are not affected.

1. **Check the provider's status page** (Anthropic, OpenAI). The metric
   `agency_llm_calls_total`, split by `outcome`, separates real errors from calls the breaker
   refused.
2. **If one provider stays down,** put the other one first: set `LLM_MODEL` and
   `LLM_FALLBACK_MODELS` in the ConfigMap, then run
   `kubectl -n agency rollout restart deployment/agency-orchestrator`.
3. **401 or 403 errors** mean the API key expired or was revoked: rotate the secret.
4. **429 errors (cost or quota):** check the usage ledger before raising any limit.
