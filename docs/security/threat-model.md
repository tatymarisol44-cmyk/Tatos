# Threat model

Scope: the API, console and background jobs of `agency-orchestrator`, as deployed by the `gke` overlay. The method is STRIDE per trust boundary. Each threat names its control and the evidence that the control works, as a test or a file. A threat with no evidence is listed as open.

## Assets

| Asset | Class (`classification.py`) | Worst outcome |
|---|---|---|
| Psychotherapy notes, clinical record | PSYCHOTHERAPY / HEALTH | disclosure to anyone but the author or care team |
| Patients' contact data, opt-outs | PERSONAL | re-identification; unwanted contact |
| Crisis alerts | HEALTH | a person in crisis is not attended |
| Keys, tokens, secret references | CREDENTIAL | takeover of a tenant |
| Audit trail | AUDIT | undetected tampering |
| Model and messaging spend | — | denial of wallet |

## Trust boundaries

1. Internet → Gateway (TLS, HSTS) → API pod.
2. API pod → model providers, Meta/Telegram/TikTok, SMTP.
3. API pod → Postgres, Qdrant, Redis (inside the cluster, behind NetworkPolicies).
4. Tenant A ↔ tenant B, inside the same pods and the same database.
5. Staff ↔ patient, inside the same tenant.
6. Text in documents, messages and model output ↔ instructions (prompt injection).

## Threats and controls

| # | STRIDE | Threat | Control | Evidence |
|---|---|---|---|---|
| T1 | Spoofing | Stolen staff key or token | Per-person keys; SSO with mandatory MFA; maximum session age; sign-out everywhere; revocation | `tests/test_oidc.py`, ADR 0018 |
| T2 | Spoofing | Forged webhook (WhatsApp, Telegram) | HMAC signature (`X-Hub-Signature-256`) and secret token, checked before parsing | `tests/test_inbound.py` |
| T3 | Tampering | Editing the audit trail with database access | Hash chain plus hourly anchors outside the database | `agency audit-verify --anchors`; **open**: WORM storage for the anchors (decision pending) |
| T4 | Info disclosure | Tenant B reads tenant A | Tenant taken from the credential, never from the request; every query filtered by tenant; every primary key starts with tenant | `tests/test_tenant_isolation_matrix.py` (every operation, mutation-checked) |
| T5 | Info disclosure | A patient reads another patient | Patient keys open only `/v1/me`; patient threads namespaced | `test_patient_keys_never_cross_patients` |
| T6 | Info disclosure | A psychotherapy note reaches RAG, a model, a campaign or an export | Data classes derive every sink; notes are author-only; personal identifiers refused from the knowledge base | `tests/test_classification.py`, `tests/test_clinical_records.py` |
| T7 | Info disclosure | Personal data in logs or traces | Redaction when each log record is created; the collector drops prompt and answer attributes | `test_logs_never_hold_personal_data` |
| T8 | Info disclosure | A leaked backup re-identifies people | Erasure leaves no contact data; random marketing ids; HMAC pseudonyms whose key is kept out of the database | `tests/test_reidentification.py` |
| T9 | Info disclosure | Metrics expose tenants | Separate port 9464, reachable only from the Prometheus namespaces; labels hold route templates, not ids | `tests/test_observability.py`, `tests/test_metrics.py` |
| T10 | Elevation | Prompt injection in a question or document makes the model act | The model proposes only; it has no tools that write; risky answers are held for a human; injection patterns are refused at upload | ADR 0008/0009, `tests/test_guardrails.py` |
| T11 | Elevation | Reception approves its own clinical answer | Role checks (reviewer); admin roles from the identity provider are audited | `tests/test_business_api.py` |
| T12 | DoS | Chat spike exhausts the pod | Per-pod limit on model routes (503 busy); body size limit; per-tenant rate limit | `tests/test_backpressure.py`, `docs/load-test.md` |
| T13 | DoS | Model provider outage | Circuit breaker; non-model routes keep working | `tests/test_resilience.py` |
| T14 | DoS / spend | Denial of wallet through the model | Per-tenant rate limit; per-pod backpressure; per-request usage ledger | **partly open**: no per-tenant monthly cap on model spend, and no cost alert |
| T15 | Repudiation | "I did not approve that answer" | Every decision audited with the person's identity (SSO principal, not a shared key) | `tests/test_governance.py` |
| T16 | Supply chain | Tampered dependency or image | Locked dependencies; Dependabot; Trivy; SHA-pinned actions; signed images verified before deploying; gitleaks over the whole history | `.github/workflows/ci.yml`, `deploy-gke.yml` |
| T17 | Info disclosure | Clinical answers cached by a browser or proxy | `Cache-Control: no-store`, HSTS, strict CSP, closed CORS | `tests/test_edge.py` |

## Open items

- **T3:** WORM bucket for audit anchors and dumps. The Terraform in `deploy/terraform` creates it with a retention lock; the owner still has to apply it.
- **T14:** per-tenant monthly model budget.
- **Not covered by tests:** side channels (timing), and the security of the identity provider itself.
- **External assessment:** see [pentest-scope.md](pentest-scope.md).
