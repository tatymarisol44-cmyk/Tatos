# ADR 0016: HTTPS gateway and edge headers

**Status:** accepted (2026-10-07). The domain and its certificate are pending (owner's decision A9).

## Context

Production runs on GKE (owner's decision A8). The API had no public entry point: the Service is ClusterIP, there was no Ingress or Gateway, so nothing terminated TLS for clients, and nothing told browsers to stay on HTTPS. The database already required TLS (`sslmode`), the API already authenticated every call with per-person keys, rate-limited per tenant and verified webhook signatures. The owner asked the system to be built on these principles: SQL-injection prevention, caching, migrations, webhooks, rate limiting, MCP, an API gateway, Docker, cloud, distributed systems, clean architecture, authentication, SSL/TLS and CORS.

## Decision

1. **A GKE Gateway is the API gateway** (`deploy/k8s/overlays/gke/gateway.yaml`): GatewayClass `gke-l7-global-external-managed`, a global external Application Load Balancer. TLS uses a Google-managed certificate from Certificate Manager, attached with the `networking.gke.io/certmap` annotation, so no certificate or key lives in the cluster. An HTTP listener exists only to answer `301` to HTTPS (HTTPRoute with a RequestRedirect filter bound to the `http` listener); only the `https` listener routes to the API. Read on 2026-10-07 in GKE's Gateway guides.
2. **HSTS** (`HSTS_MAX_AGE_SECONDS`, one year in the GKE overlay, off by default for local HTTP): once a browser sees it, it refuses plain HTTP to the host.
3. **CORS is closed by default.** `CORS_ALLOWED_ORIGINS` takes exact origins only: no `*`, no path, `https` (plain `http` only for localhost, and refused in production). Credentials are not allowed: keys travel in `X-API-Key`, never in cookies.
4. **Edge headers on every response** (`api/edge.py`): `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, `X-Frame-Options: DENY`, and `Cache-Control: no-store` on `/v1/...`, because API answers can hold health data, which no browser or proxy may keep. Routes that set their own Cache-Control (the SSE stream) keep it.
5. **Validation in CI.** kubeconform validates the Gateway API kinds against the public CRD schema catalog, for every overlay, including `ci` and `gke`.

## Consequences

- Clients reach the API only over HTTPS, and the certificate renews itself.
- Caching is applied where it is safe, not to answers about people: configuration and packs are cached in-process; API responses are explicitly not cacheable.
- Not decided here: the domain (A9), a static IP, Cloud Armor (WAF and IP rate limits at the edge, in front of the per-tenant limits), and an SSL policy for a minimum TLS version (not confirmed in the docs read).
