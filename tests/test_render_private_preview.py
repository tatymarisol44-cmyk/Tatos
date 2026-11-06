"""Dedicated, authenticated Render staging: no anonymous bootstrap or cross-origin use."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from tools.render_preview import make_protected_app, validated_origin

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
