"""Single sign-on for staff with OpenID Connect (ADR 0018, audit 2026-10-08).

Staff sign in at the clinic's identity provider (Google Workspace / Identity Platform,
Microsoft Entra ID, Auth0, Keycloak...), which enforces passwords and MFA. The console
sends the ID or access token as `Authorization: Bearer <JWT>`; this module verifies it:

- signature with the provider's published keys (JWKS, cached), asymmetric algorithms
  only: a token signed with "none" or an HMAC secret is refused;
- issuer, audience, expiry, not-before, with 60 s of clock leeway;
- MFA: `amr` must name a second factor (or `acr` be an accepted level, or the configured
  `oidc_mfa_claim` be non-empty) when required;
- session age: the sign-in (`auth_time`, else `iat`) is at most `oidc_max_session_hours`
  old, whatever the token's own lifetime;
- tenant and roles from configured claims (dotted paths reach nested claims, e.g. the
  custom claims of Google Identity Platform); the tenant must be one this deployment serves.

The person is then provisioned just in time as a staff principal (`PrincipalStore.
from_identity`), so revocation, "sign out everywhere" and the audit trail work exactly
as for staff keys. Per-person keys remain for service integrations and as break-glass
access (docs/runbooks/break-glass.md)."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import jwt

from orchestrator.auth import Role
from orchestrator.config import Settings

ASYMMETRIC = ["RS256", "RS384", "RS512", "PS256", "ES256", "ES384", "EdDSA"]


def _claim(claims: dict[str, Any], path: str) -> Any:
    """`a.b` reads claims["a"]["b"]; a missing step gives None."""
    value: Any = claims
    for step in path.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(step)
    return value


class IdentityError(Exception):
    """The token is not acceptable; the message says why (safe to return: no secrets)."""


@dataclass(frozen=True)
class Identity:
    tenant: str
    id: str
    roles: frozenset[str]
    issued_at: float  # the token's iat: compared with the principal's sign-out mark
    mfa: bool


class OIDCVerifier:
    def __init__(self, settings: Settings, jwks: Any = None) -> None:
        if not settings.oidc_issuer or not settings.oidc_audience:
            raise ValueError("OIDC needs OIDC_ISSUER and OIDC_AUDIENCE")
        self.settings = settings
        self.issuer = settings.oidc_issuer.rstrip("/")
        url = settings.oidc_jwks_url or self._discover_jwks_url()
        # PyJWKClient caches the key set and refetches on an unknown `kid` (key rotation).
        self.jwks = jwks or jwt.PyJWKClient(url, cache_keys=True, lifespan=3600, timeout=5)

    def _discover_jwks_url(self) -> str:
        import httpx

        response = httpx.get(f"{self.issuer}/.well-known/openid-configuration", timeout=5)
        response.raise_for_status()
        return str(response.json()["jwks_uri"])

    def verify(self, token: str, known_tenants: set[str]) -> Identity:
        """Blocking (the first call may fetch keys): run it in a worker thread."""
        s = self.settings
        try:
            header = jwt.get_unverified_header(token)
            if header.get("alg") not in ASYMMETRIC:
                raise IdentityError("token algorithm not accepted")
            key = self.jwks.get_signing_key_from_jwt(token).key
            claims: dict[str, Any] = jwt.decode(
                token,
                key=key,
                algorithms=ASYMMETRIC,
                audience=s.oidc_audience,
                issuer=self.issuer,
                leeway=60,
                options={"require": ["exp", "iat", "iss", "aud", "sub"]},
            )
        except IdentityError:
            raise
        except jwt.ExpiredSignatureError as exc:
            raise IdentityError("token expired") from exc
        except jwt.PyJWTError as exc:
            raise IdentityError(f"invalid token ({type(exc).__name__})") from exc

        signed_in = float(claims.get("auth_time") or claims["iat"])
        if time.time() - signed_in > s.oidc_max_session_hours * 3600:
            raise IdentityError("session too old: sign in again")
        amr = {str(m).lower() for m in claims.get("amr") or []}
        mfa = bool(amr & {m.lower() for m in s.oidc_mfa_amr}) or (
            str(claims.get("acr", "")) in s.oidc_mfa_acr
        )
        if s.oidc_mfa_claim and _claim(claims, s.oidc_mfa_claim):
            mfa = True
        if s.oidc_require_mfa and not mfa:
            raise IdentityError("multi-factor authentication required")

        ident = _claim(claims, s.oidc_id_claim)
        if not isinstance(ident, str) or not ident:
            raise IdentityError(f"token has no {s.oidc_id_claim!r} claim")
        if s.oidc_id_claim == "email" and claims.get("email_verified") is not True:
            raise IdentityError("e-mail not verified by the identity provider")
        tenant = _claim(claims, s.oidc_tenant_claim)
        if not isinstance(tenant, str) or tenant not in known_tenants:
            raise IdentityError("not a member of a tenant served here")
        raw_roles = _claim(claims, s.oidc_roles_claim) or []
        if isinstance(raw_roles, str):
            raw_roles = raw_roles.split()
        roles = frozenset(str(r) for r in raw_roles) & {r.value for r in Role}
        if not roles:
            raise IdentityError("no staff role granted by the identity provider")
        return Identity(tenant, ident.lower(), roles, float(claims["iat"]), mfa)
