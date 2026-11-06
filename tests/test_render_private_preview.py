"""Dedicated, authenticated Render staging: no anonymous bootstrap or cross-origin use."""

import re
import secrets
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from tools.render_preview import make_protected_app, validated_origin

from orchestrator.staff_login import totp_at, unb32

ORIGIN = "https://tatos-pruebas-test.onrender.com"


@pytest.mark.parametrize(
    "origin",
    [
        "https://agency-api-irim.onrender.com",
        "http://tatos-pruebas-test.onrender.com",
        "https://tatos-pruebas-test.example.com",
        ORIGIN + "@attacker.example",
        ORIGIN + "/some-path",
        ORIGIN + "?token=not-used",
    ],
)
def test_production_and_non_preview_origins_are_rejected(origin: str) -> None:
    with pytest.raises(ValueError):
        validated_origin(origin)


def test_private_preview_requires_password_before_constructing_app(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        make_protected_app(tmp_path, ORIGIN, "")


def test_preview_protects_html_scripts_data_and_mutations(tmp_path: Path) -> None:
    password = "a" * 40  # generated credentials are used only in the actual deployment
    app = make_protected_app(tmp_path, ORIGIN, password)
    with TestClient(app, base_url=ORIGIN) as client:
        health = client.get("/preview-healthz")
        assert health.status_code == 200 and health.json()["real_operations"] is False
        for path in (
            "/lab",
            "/app",
            "/mi/",
            "/lab/entry/clinician.js",
            "/static/consultorio.js",
            "/v1/session",
        ):
            result = client.get(path)
            assert result.status_code == 401 and result.headers["cache-control"] == "no-store"
        assert client.post("/v1/tax-trials/documents", json={}).status_code == 401
        assert client.get("/lab", auth=("wrong", password)).status_code == 401
        assert client.get("/lab", auth=("revisora", password)).status_code == 200
        assert (
            client.get(
                "/lab/entry/clinician.js",
                auth=("revisora", password),
                headers={"Sec-Fetch-Site": "cross-site"},
            ).status_code
            == 403
        )
        assert (
            client.get(
                "/lab", auth=("revisora", password), headers={"Origin": "https://attacker.example"}
            ).status_code
            == 403
        )
        assert (
            client.get(
                "/lab", auth=("revisora", password), headers={"Host": "attacker.example"}
            ).status_code
            == 403
        )
        script = client.get("/lab/entry/clinician.js", auth=("revisora", password))
        assert script.status_code == 200
        assert script.headers["cross-origin-resource-policy"] == "same-origin"
        assert script.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("site", ["same-site", "cross-site"])
def test_invitation_document_navigation_is_allowed_but_data_and_writes_are_not(
    tmp_path: Path, site: str
) -> None:
    password = secrets.token_urlsafe(36)
    app = make_protected_app(tmp_path, ORIGIN, password)
    navigation = {
        "Sec-Fetch-Site": site,
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Dest": "document",
    }
    with TestClient(app, base_url=ORIGIN) as client:
        # The fragment remains in the browser; no invitation token is sent in HTTP.
        assert client.get("/app", headers=navigation).status_code == 401
        client.auth = ("revisora", password)
        for path in ("/app", "/lab", "/mi/"):
            assert client.get(path, headers=navigation).status_code == 200
        for path in ("/lab/entry/clinician.js", "/v1/session", "/static/consultorio.js"):
            assert client.get(path, headers=navigation).status_code == 403
        assert client.post("/v1/auth/link", headers=navigation, json={}).status_code == 403
        assert (
            client.get("/app", headers={**navigation, "Sec-Fetch-Dest": "iframe"}).status_code
            == 403
        )
        assert (
            client.get("/app", headers={**navigation, "Sec-Fetch-Mode": "cors"}).status_code == 403
        )
        assert (
            client.get(
                "/app", headers={**navigation, "Origin": "https://untrusted.example"}
            ).status_code
            == 403
        )


def test_protected_preview_invitation_enrolls_only_a_temporary_reception_member(
    tmp_path: Path,
) -> None:
    password = secrets.token_urlsafe(36)
    app = make_protected_app(tmp_path, ORIGIN, password)
    with TestClient(app, base_url=ORIGIN) as client:
        client.auth = ("revisora", password)
        script = client.get("/lab/entry/clinician.js").text
        match = re.search(r'setItem\("consultorio.key","([^\"]+)"\)', script)
        assert match is not None
        admin = {"X-API-Key": match[1]}
        member = client.post(
            "/v1/admin/team",
            headers=admin,
            json={
                "name": "QA Invitation",
                "email": "invitation.qa@example.com",
                "roles": ["reception"],
            },
        )
        assert member.status_code == 201
        token = member.json()["link"].split("#clave=", 1)[1]
        passphrase = secrets.token_urlsafe(32)
        started = client.post("/v1/auth/link", json={"token": token, "password": passphrase})
        assert started.status_code == 200
        started_body = started.json()
        code = totp_at(unb32(started_body["secret"]), int(time.time() // 30))
        enrolled = client.post(
            "/v1/auth/enroll", json={"challenge": started_body["challenge"], "code": code}
        )
        assert enrolled.status_code == 200
        reception = {"X-API-Key": enrolled.json()["key"]}
        session = client.get("/v1/session", headers=reception)
        assert session.status_code == 200
        assert session.json()["roles"] == ["reception"]
        assert client.get("/v1/admin/team", headers=reception).status_code == 403
        assert client.get("/v1/auth/register").status_code == 403
        assert (
            client.post("/v1/auth/link", json={"token": token, "password": passphrase}).status_code
            == 410
        )
        # Password sign-in is still MFA-gated and limited to the temporary store.
        challenge = client.post(
            "/v1/auth/sign-in", json={"email": "invitation.qa@example.com", "password": passphrase}
        )
        assert challenge.status_code == 200
        assert "key" not in challenge.json()
