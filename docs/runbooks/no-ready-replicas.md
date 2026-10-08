# No ready API replica, or restarts

1. **Look at the pods:** `kubectl -n agency get pods -l app.kubernetes.io/name=agency-orchestrator`,
   then `kubectl -n agency describe pod <pod>`.
2. **`/readyz` answers 503 `database unavailable`.** Possible causes:
   - Cloud SQL is down;
   - `DATABASE_URL` is wrong;
   - TLS fails (`sslmode=require` is mandatory in prod);
   - the NetworkPolicy blocks egress to port 5432.
3. **The `migrate` init container fails.** Read `kubectl -n agency logs <pod> -c migrate`. A
   failed migration leaves the old pods serving (`maxUnavailable: 0`). Fix forward, or roll
   back the image.
4. **Crash loop right after a deploy:** run `kubectl -n agency rollout undo deployment/agency-orchestrator`.
