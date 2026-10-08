# Running an incident

1. **Declare.** Whoever is paged owns the incident until they hand it over explicitly.
   Open a dated note (`incident-YYYY-MM-DD-short-name`) and record every action with its time.
2. **Classify.**

   | Severity | Meaning | Examples |
   |---|---|---|
   | SEV-1 | Patient safety, or personal or health data possibly exposed | a crisis alert unattended; a cross-tenant leak; leaked credentials |
   | SEV-2 | The product is down or wrong for clinics | no ready replicas; a fast error-budget burn |
   | SEV-3 | Degraded, with a workaround | a model outage (assistant only); latency; a stuck outbox |

3. **Stabilise before you fix.** Prefer
   `kubectl -n agency rollout undo deployment/agency-orchestrator` to a hot patch. Never turn
   off the audit, the tenant checks or the guardrails to get things working.
4. **SEV-1 with data:** follow [data-breach.md](data-breach.md) at once, in parallel.
5. **Close** with a blameless review within 5 working days. Cover the timeline, the impact
   (tenants, patients, duration), the root cause, and the test or alert that would have
   caught it.
