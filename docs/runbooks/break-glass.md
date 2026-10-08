# Break-glass access

Use this when the identity provider is down, or nobody with the `admin` role can sign in,
and something must be done now: a crisis alert, revoking a compromised account, a legal
request with a deadline.

1. **Two people.** One operator uses the access; a second person (the clinic owner or the
   platform lead) agrees first and watches. Write down both names, the time and the reason.
2. **Use the tenant's service key.** It lives in the `agency-orchestrator-secrets` Secret
   (`API_KEYS`). Read it with `kubectl -n agency get secret agency-orchestrator-secrets -o jsonpath='{.data.API_KEYS}' | base64 -d`.
   Reading that Secret is itself logged by the cluster's audit log.
3. **Do only the task.** Every action is audited as `service:<tenant>`.
4. **Close.**
   - Rotate that service key. Generate a new one, update the Secret, then run
     `kubectl -n agency rollout restart deployment/agency-orchestrator`.
   - Attach the list of audit events from the break-glass window (`GET /v1/audit`) to the
     incident note.
