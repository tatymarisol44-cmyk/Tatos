# Road to production

The steps in order. Each one names who does it. "Repo" means the step is already done and tested in this repository. "Owner" means it needs the owner's accounts, money, signature or a decision; those decisions are tracked in `docs/private/DECISIONS-PENDING.md`.

## 1. Engineering gates (repo: done)

All of these run in CI on every commit:

| Gate | Evidence |
|---|---|
| Tenant isolation across every API operation | `tests/test_tenant_isolation_matrix.py` |
| Data classification drives every sink; no PHI in logs or prompts | `tests/test_classification.py` |
| Re-identification of a leaked database | `tests/test_reidentification.py` |
| SSO with mandatory MFA, sign-out everywhere | `tests/test_oidc.py`, ADR 0018 |
| Resilience: model, vector store and database outages | `tests/test_resilience.py` |
| Backpressure and the measured breaking point | `tests/test_backpressure.py`, `docs/load-test.md` |
| SLOs, burn-rate alerts (unit-tested), runbooks | `docs/slo.md`, `deploy/monitoring/` |
| Backup and restore drill; knowledge index rebuilt from the database | `tests/test_backup_restore.py`, `tests/test_knowledge.py` |
| WCAG 2.1 AA (axe), keyboard use, AI provenance on every answer | `tests/test_console_e2e.py`, `docs/accessibility.md` |
| Architecture rules on the import graph | `tests/test_architecture.py` |
| Supply chain: signed images, SBOM, provenance, history secret scan, pinned actions | `.github/workflows/ci.yml` |
| Infrastructure as code, validated | `deploy/terraform/` |
| Clinical workspace: tests designed by the psychologist, clinical files, follow-up (ADR 0019) | `tests/test_instruments.py`, `tests/test_clinical_files.py`, `tests/test_followup.py` |
| Agenda without double booking, calendar feeds, reminders, WhatsApp assistant without medical advice (ADR 0020) | `tests/test_agenda.py`, `tests/test_reminders.py`, `tests/test_whatsapp_assistant.py` |
| Monthly model spend cap per practice | `tests/test_spend.py` |
| One-command MVP demo in Codespaces | `scripts/demo-codespaces.sh`, `tests/test_demo.py`, `docs/DEMO.md` |
| Clinical data is need to know: admin and service keys cannot read it | `tests/test_need_to_know.py` |
| Register of rights requests and breaches with legal deadlines, paging before they lapse | `tests/test_privacy_cases.py`, `docs/runbooks/privacy-deadline.md` |
| Deploy rehearsal on kind with a forced rollback, green on every push (first green 2026-10-09) | `.github/workflows/deploy.yml` |
| Only attested images run on GKE: CI signature verified, digest copied to Artifact Registry, Binary Authorization enforced (O4, O6) | `.github/workflows/deploy-gke.yml`, `deploy/terraform/supply_chain.tf` |
| Platform paging rendered from env, checked with `amtool` (O1) | `deploy/monitoring/render_alertmanager.py` |

## 2. Repository and CI live (done 2026-10-08; one owner step left)

1. ~~Push to the remote~~ and ~~a green CI~~: done; CI and the Deploy rehearsal are green.
2. GitHub settings (O5), owner only:
   - branch protection on `main`: PR required, plus the checks `quality`, `test`, `security`, `manifests` and `image`;
   - environments `staging` and `production`, with required reviewers on `production`.

## 3. Cloud (owner, about 2 hours, needs billing)

1. Region decided (O9): `us-east1`, with backups in `us-central1`.
2. Create two GCP projects, staging and production (O3, decided: separate projects), with billing and a budget alert.
3. Apply `deploy/terraform` in each project (`deploy/terraform/README.md`), then copy the outputs into the GitHub environments, including `AR_REPOSITORY`, `BINAUTHZ_ATTESTOR` and `BINAUTHZ_KEY_VERSION`.
4. Create the Kubernetes Secret per environment: API keys, database URLs through the private IP with `sslmode=require`, `PSEUDONYM_KEY` from Secret Manager, and the provider keys.
5. Domain, DNS and the certificate map for the Gateway (A9).
6. Alert routing (O1, decided): create a Telegram bot and group for the platform on-call, then run `deploy/monitoring/render_alertmanager.py | kubectl apply -f -` with its token. Add the SMTP variables once a provider is chosen (P11).
7. Staff single sign-on (O7, decided: Google Identity Platform): enable it in each project, turn on TOTP MFA, and set the `OIDC_*` values from ADR 0018.

## 4. First deploy to staging (repo + owner)

1. Merge to `main`. CI then runs the kind rehearsal, and the staging deploy follows.
2. Run the restore drill on staging and write it in `docs/runbooks/restore.md`.
3. Run the load test against staging with real provider latency and record it in `docs/load-test.md`.
4. Load synthetic tenants and test patients only.

## 5. Independent checks (owner commissions them)

1. Penetration test on staging (O8): `docs/security/pentest-scope.md`. Fix the critical and high findings, add a regression test for each, and have them retested.
2. Manual accessibility review: `docs/accessibility.md`.
3. Legal review in Ecuador (L1–L5; the lawyer's questions in `docs/private/`):
   - LOPDP basis and data residency;
   - the processing agreement with each clinic;
   - health advertising rules;
   - deadlines for breach notification.
4. Contracts with the AI providers: a data processing agreement, and zero data retention where available. This is **required before real patients use WhatsApp**, because the assistant sends the message text to the model (ADR 0020).
5. Meta approvals for the two WhatsApp templates: the appointment reminder and the staff alert.

## 6. Pilot (owner + one clinic)

1. Production deploy: CI → staging → approval → production, the same signed digest.
2. Lock the retention of the audit-anchor bucket (`-var lock_retention=true`). This is irreversible.
3. One clinic, real patients, the review queue on for every clinical answer, a weekly SLO review.
4. Leave the pilot only when the SLOs held for 4 weeks and no critical or high finding is open.
