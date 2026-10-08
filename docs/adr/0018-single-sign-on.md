# ADR 0018: Single sign-on with MFA for staff

**Status:** accepted (2026-10-08, audit 2026-10-08, item 5). The identity provider itself is still open (owner decision O7).

## Context

Today every staff member authenticates with a personal key (`sk_`, ADR 0009, decision A in the first audit). That works, but a clinic SaaS that holds health data also needs:

- a second factor;
- sessions that expire;
- ending the session of a lost device;
- leavers losing access without someone remembering to revoke a key;
- one place where the clinic manages its people.

An identity provider already does all of that.

## Decision

1. **OpenID Connect bearer tokens** (`Authorization: Bearer <JWT>`), accepted alongside keys. They work with any OIDC provider: Google Workspace or Identity Platform, Microsoft Entra ID, Auth0, Keycloak. Configuration lives in `OIDC_*`. The feature is off unless `OIDC_ISSUER` is set.
2. **What the token must prove** (`orchestrator/oidc.py`):
   - a signature from the provider's published keys (JWKS, cached, refreshed on key rotation), with asymmetric algorithms only;
   - issuer, audience, expiry and not-before, with 60 s of leeway;
   - **MFA**: `amr` names a second factor, or `acr` is an accepted level. This is mandatory: `APP_ENV=prod` refuses to start with `OIDC_REQUIRE_MFA=false`;
   - **session age**: the sign-in is at most `OIDC_MAX_SESSION_HOURS` (12) old, whatever the token's own lifetime;
   - a verified e-mail (or the configured id claim);
   - a tenant this deployment serves (claim `tenant`) and at least one staff role (claim `roles`).
3. **Just-in-time provisioning.** On first sign-in, the person becomes a staff principal (`principals`, `created_by = oidc`), audited as `principal.staff.provisioned`. On every sign-in their roles follow the provider, and a change is audited as `principal.roles_synced`. The audit names the person, never a shared identity.
4. **Ending access.**
   - `POST /v1/admin/principals/{id}/sign-out` refuses every token issued before now. The person can sign in again, so this is the remedy for a lost device.
   - `DELETE /v1/admin/principals/{id}` refuses the identity for good.
   - Removing the person at the provider stops new tokens within their lifetime.
5. **Keys remain** for service integrations (a clinic's own backend) and as break-glass access when the provider is down (`docs/runbooks/break-glass.md`). Every action taken with a service key is audited as `service:<tenant>`.
6. **Patients** keep their access links (`pk_`) for now. A patient login with the provider is part of the patient app (PWA), not of this decision.

## Consequences

- MFA, password policy, device posture and leavers are handled where clinics already manage them.
- The rate limit, the tenant isolation matrix and the role checks apply unchanged: an SSO principal is a staff principal.
- The web console still asks for a key. Signing in with the provider (authorization code + PKCE in the browser) is the next console step, once O7 is decided.
- Not built: SCIM provisioning (accounts appear on first sign-in instead) and step-up authentication for the most sensitive actions.
