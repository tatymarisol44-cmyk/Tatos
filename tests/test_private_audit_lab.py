"""The combined preview seeds the actual app and refuses foreign browser origins.

Written for the next reviewed native run; synthetic data only.
"""

import importlib.util
import re
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient


def lab_module() -> Any:
    path = Path(__file__).resolve().parents[1] / "tools/private_audit_lab.py"
    spec = importlib.util.spec_from_file_location("tatos_private_audit_lab", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_private_lab_has_fake_state_real_app_and_no_cross_origin_bootstrap(tmp_path: Path) -> None:
    lab = lab_module()
    with TestClient(lab.make_app(tmp_path), base_url=lab.BASE) as client:
        assert client.get("/lab").status_code == 200
        assert client.get("/lab", headers={"Host": "untrusted.example"}).status_code == 403
        assert (
            client.get(
                "/lab/entry/clinician.js", headers={"Origin": "https://untrusted.example"}
            ).status_code
            == 403
        )
        # This is a disposable lab identity; never persist or print it in evidence.
        script = client.get("/lab/entry/clinician.js").text
        match = re.search(r'setItem\("consultorio.key","([^"]+)"\)', script)
        assert match is not None
        headers = {"X-API-Key": match[1]}
        session = client.get("/v1/session", headers=headers).json()
        assert session["clinician"] is True
        assert len(client.get("/v1/billing/orders", headers=headers).json()) == 4
        assert (
            client.get("/v1/surveys/configuration", headers=headers).json()["settings"]["enabled"]
            is True
        )
        assert len(client.get("/v1/marketing/workflows", headers=headers).json()) == 1
        assert (
            client.get("/v1/tax-trials/issuers", headers=headers).json()["sri_authorised"] is False
        )
        assert b"private-tools.js" in client.get("/app").content
        assert b"patient-surveys.js" in client.get("/mi/").content
        assert client.post("/v1/auth/sign-in").status_code == 422
        assert client.post("/v1/auth/register").status_code == 403
