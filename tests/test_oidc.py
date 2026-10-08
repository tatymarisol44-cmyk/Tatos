"""Single sign-on with OpenID Connect (ADR 0018): signature, issuer, audience, expiry, MFA,
session age, tenant and roles from claims, just-in-time provisioning, role sync,
"sign out everywhere" and revocation. A synthetic identity provider signs the tokens."""

from __future__ import annotations

import time
from collections.abc import Iterator
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from orchestrator.api.app import create_app
from orchestrator.catalog import Catalog
from orchestrator.config import Settings
from orchestrator.llm import FakeLLM
from orchestrator.oidc import OIDCVerifier
from orchestrator.service import Orchestrator

ISSUER = "https://idp.example.test"
AUDIENCE = "agency-console"
ADMIN = {"X-API-Key": "test-key"}
_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


class StaticJWKS:
    """Stands in for PyJWKClient: the provider's published public key."""

    def __init__(self) -> None:
        jwk = jwt.algorithms.RSAAlgorithm.to_jwk(_KEY.public_key(), as_dict=True)
        self.key = jwt.PyJWK.from_dict({**jwk, "kid": "k1", "alg": "RS256"})

    def get_signing_key_from_jwt(self, token: str) -> Any:
        if jwt.get_unverified_header(token).get("kid") != "k1":
            raise jwt.PyJWKClientError("unknown kid")
        return self.key


def token(key: Any = _KEY, alg: str = "RS256", **overrides: Any) -> str:
    now = int(time.time())
    claims: dict[str, Any] = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": "u-1",
        "email": "Dra.Vera@Clinica.test",
        "email_verified": True,
        "tenant": "acme",
        "roles": ["reception"],
        "amr": ["pwd", "otp"],
        "iat": now,
        "exp": now + 600,
    }
    claims.update(overrides)
    claims = {k: v for k, v in claims.items() if v is not None}
    return jwt.encode(claims, key, algorithm=alg, headers={"kid": "k1"})


def bearer(tok: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {tok}"}


@pytest.fixture
def sso(settings: Settings, catalog: Catalog) -> Iterator[TestClient]:
    settings.oidc_issuer = ISSUER
    settings.oidc_audience = AUDIENCE
    settings.oidc_jwks_url = f"{ISSUER}/jwks"
    orch = Orchestrator(settings, catalog=catalog, llm=FakeLLM())
    with TestClient(create_app(settings, orch)) as c:
        c.app.state.oidc = OIDCVerifier(settings, jwks=StaticJWKS())  # type: ignore[attr-defined]
        yield c


def test_a_valid_token_opens_the_api_with_its_roles_only(sso: TestClient) -> None:
    body = {"id": "p-1", "display_name": "Ana"}
    created = sso.post("/v1/crm/patients", json=body, headers=bearer(token()))
    assert created.status_code == 201
    assert sso.get("/v1/crm/patients", headers=bearer(token())).status_code == 200
    # Reception is not admin: key management stays closed.
    assert sso.get("/v1/admin/principals", headers=bearer(token())).status_code == 403
    principals = sso.get("/v1/admin/principals", headers=ADMIN).json()
    [me] = [p for p in principals if p["id"] == "dra.vera@clinica.test"]  # lower-cased
    assert me["kind"] == "staff" and me["roles"] == ["reception"] and me["created_by"] == "oidc"
    audit = sso.get("/v1/audit", headers=ADMIN).text
    assert "principal.staff.provisioned" in audit
    # The audit names the person, not a shared service identity.
    assert '"actor":"dra.vera@clinica.test"' in audit.replace(" ", "")


@pytest.mark.parametrize(
    ("tok", "reason"),
    [
        (lambda: token(amr=["pwd"]), "multi-factor"),
        (lambda: token(aud="another-app"), "InvalidAudience"),
        (lambda: token(iss="https://evil.test"), "InvalidIssuer"),
        (lambda: token(exp=int(time.time()) - 120), "expired"),
        (lambda: token(key=_OTHER_KEY), "InvalidSignature"),
        (lambda: token(key="shared-secret-of-32-bytes-or-more!!", alg="HS256"), "algorithm"),
        (lambda: token(tenant="umbrella"), "not a member"),
        (lambda: token(email_verified=False), "not verified"),
        (lambda: token(roles=["superuser"]), "no staff role"),
        (lambda: token(auth_time=int(time.time()) - 13 * 3600), "session too old"),
    ],
)
def test_unacceptable_tokens_are_refused(sso: TestClient, tok: Any, reason: str) -> None:
    response = sso.get("/v1/crm/patients", headers=bearer(tok()))
    assert response.status_code == 401
    assert reason in response.json()["detail"]
    assert response.headers["WWW-Authenticate"] == "Bearer"


def test_unsigned_tokens_are_refused(sso: TestClient) -> None:
    unsigned = jwt.encode({"iss": ISSUER, "aud": AUDIENCE}, None, algorithm="none")
    assert sso.get("/v1/crm/patients", headers=bearer(unsigned)).status_code == 401


def test_the_token_decides_the_tenant(sso: TestClient) -> None:
    sso.post("/v1/crm/patients", json={"id": "p-acme", "display_name": "A"}, headers=ADMIN)
    globex = bearer(token(tenant="globex", email="x@globex.test"))
    assert sso.get("/v1/crm/patients", headers=globex).json() == []
    assert sso.get("/v1/crm/patients/p-acme", headers=globex).status_code == 404


def test_roles_follow_the_identity_provider(sso: TestClient) -> None:
    assert sso.get("/v1/admin/principals", headers=bearer(token())).status_code == 403
    promoted = bearer(token(roles=["admin"]))
    assert sso.get("/v1/admin/principals", headers=promoted).status_code == 200
    assert "principal.roles_synced" in sso.get("/v1/audit", headers=ADMIN).text
    demoted = bearer(token(roles=["reception"]))
    assert sso.get("/v1/admin/principals", headers=demoted).status_code == 403


def test_sign_out_everywhere_and_revocation(sso: TestClient) -> None:
    old = bearer(token(iat=int(time.time()) - 30))
    assert sso.get("/v1/crm/patients", headers=old).status_code == 200
    pid = "dra.vera@clinica.test"
    assert sso.post(f"/v1/admin/principals/{pid}/sign-out", headers=ADMIN).status_code == 204
    refused = sso.get("/v1/crm/patients", headers=old)
    assert refused.status_code == 401 and "sign in again" in refused.json()["detail"]
    fresh = bearer(token(iat=int(time.time()) + 1))  # a new sign-in after the sign-out
    assert sso.get("/v1/crm/patients", headers=fresh).status_code == 200
    assert sso.delete(f"/v1/admin/principals/{pid}", headers=ADMIN).status_code == 204
    assert sso.get("/v1/crm/patients", headers=fresh).status_code == 401  # for good
    newest = bearer(token(iat=int(time.time()) + 2))
    assert sso.get("/v1/crm/patients", headers=newest).status_code == 401
    assert "principal.signed_out" in sso.get("/v1/audit", headers=ADMIN).text


def test_an_identity_cannot_take_over_a_patient_principal(sso: TestClient) -> None:
    sso.post("/v1/crm/patients", json={"id": "p-9", "display_name": "B"}, headers=ADMIN)
    access = sso.post("/v1/crm/patients/p-9/access", headers=ADMIN).json()
    hijack = bearer(token(email=access["id"]))
    assert sso.get("/v1/crm/patients", headers=hijack).status_code == 401


def test_without_sso_configured_a_bearer_token_is_refused(
    settings: Settings, catalog: Catalog
) -> None:
    orch = Orchestrator(settings, catalog=catalog, llm=FakeLLM())
    with TestClient(create_app(settings, orch)) as c:
        response = c.get("/v1/crm/patients", headers=bearer(token()))
        assert response.status_code == 401
        assert "not enabled" in response.json()["detail"]


def test_prod_refuses_single_sign_on_without_mfa(settings: Settings) -> None:
    settings.app_env = "prod"
    settings.oidc_issuer = ISSUER
    settings.oidc_audience = AUDIENCE
    settings.oidc_require_mfa = False
    with pytest.raises(RuntimeError, match="OIDC_REQUIRE_MFA"):
        create_app(settings)
