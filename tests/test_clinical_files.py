"""Clinical files: what goes in is what it claims to be, comes out identical, and only
to the people allowed to see it. Synthetic content only."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from orchestrator.api.app import create_app
from orchestrator.catalog import Catalog
from orchestrator.clinical_files import FileRejected, safe_name, sniff
from orchestrator.config import Settings
from orchestrator.llm import FakeLLM
from orchestrator.service import Orchestrator

ADMIN = {"X-API-Key": "test-key"}
PDF = b"%PDF-1.7\n% consentimiento firmado (sintetico)\n%%EOF\n"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


def test_the_content_decides_the_type() -> None:
    assert sniff(PDF) == "application/pdf"
    assert sniff(PNG) == "image/png"
    assert sniff(b"\xff\xd8\xff\xe0jpeg") == "image/jpeg"
    assert sniff(b"Informe: sin hallazgos.") == "text/plain"
    for bad in (b"MZ\x90\x00binary", b"\x00\x01\x02", b"PK\x03\x04zip"):
        with pytest.raises(FileRejected):
            sniff(bad)
    assert safe_name("../../etc/passwd") == "passwd"
    assert safe_name(r"C:\x\Consentimiento firmado.pdf") == "Consentimiento firmado.pdf"


@pytest.fixture
def client(settings: Settings, catalog: Catalog) -> Iterator[TestClient]:
    settings.clinical_file_max_bytes = 4096
    orch = Orchestrator(settings, catalog=catalog, llm=FakeLLM())
    with TestClient(create_app(settings, orch)) as c:
        c.post("/v1/crm/patients", json={"id": "p-1", "display_name": "Ana"}, headers=ADMIN)
        yield c


def staff(client: TestClient, name: str, role: str) -> dict[str, str]:
    key = client.post("/v1/admin/staff", json={"name": name, "roles": [role]}, headers=ADMIN)
    return {"X-API-Key": key.json()["key"]}


def upload(client: TestClient, headers: dict[str, str], content: bytes, **form: str) -> object:
    data = {"label": "Consentimiento informado firmado", **form}
    return client.post(
        "/v1/clinical/patients/p-1/files",
        files={"file": ("consentimiento.pdf", content, "application/pdf")},
        data=data,
        headers=headers,
    )


def test_upload_list_download_identical_and_audited(client: TestClient) -> None:
    vera = staff(client, "dra.vera", "reviewer")
    made = upload(client, vera, PDF)
    assert made.status_code == 201, made.text  # type: ignore[attr-defined]
    info = made.json()  # type: ignore[attr-defined]
    assert info["media_type"] == "application/pdf" and info["size"] == len(PDF)
    assert info["sha256"] == hashlib.sha256(PDF).hexdigest()
    listed = client.get("/v1/clinical/patients/p-1/files", headers=vera).json()
    assert [f["file_id"] for f in listed] == [info["file_id"]]
    assert "content" not in listed[0]

    got = client.get(f"/v1/clinical/files/{info['file_id']}", headers=vera)
    assert got.content == PDF  # byte for byte
    assert got.headers["content-disposition"].startswith("attachment;")
    assert got.headers["x-content-sha256"] == info["sha256"]
    assert got.headers["cache-control"] == "no-store"
    audit = client.get("/v1/audit", headers=ADMIN).text
    assert "clinical_file.uploaded" in audit and "clinical_file.read" in audit

    export = client.get("/v1/subjects/p-1/export", headers=ADMIN).json()
    assert export["clinical_files"]["files"][0]["sha256"] == info["sha256"]


def test_what_is_refused(client: TestClient) -> None:
    vera = staff(client, "dra.vera", "reviewer")
    reception = staff(client, "maria", "reception")
    # A program renamed to .pdf, an empty file, a file over the limit.
    assert upload(client, vera, b"MZ\x90\x00 not a pdf").status_code == 422  # type: ignore[attr-defined]
    assert upload(client, vera, b"").status_code == 422  # type: ignore[attr-defined]
    assert upload(client, vera, PDF + b"x" * 5000).status_code in (413, 422)  # type: ignore[attr-defined]
    # Reception has no access to clinical files at all.
    assert upload(client, reception, PDF).status_code == 403  # type: ignore[attr-defined]
    assert client.get("/v1/clinical/patients/p-1/files", headers=reception).status_code == 403
    missing = client.post(
        "/v1/clinical/patients/p-404/files",
        files={"file": ("x.pdf", PDF, "application/pdf")},
        data={"label": "x"},
        headers=vera,
    )
    assert missing.status_code == 404


def test_author_only_files_stay_with_their_author(client: TestClient) -> None:
    vera = staff(client, "dra.vera", "reviewer")
    ruiz = staff(client, "dr.ruiz", "reviewer")
    private = upload(client, vera, PDF, access="author_only").json()  # type: ignore[attr-defined]
    shared = upload(client, vera, PNG).json()  # type: ignore[attr-defined]
    assert [
        f["file_id"] for f in client.get("/v1/clinical/patients/p-1/files", headers=ruiz).json()
    ] == [shared["file_id"]]
    assert client.get(f"/v1/clinical/files/{private['file_id']}", headers=ruiz).status_code == 404
    assert client.get(f"/v1/clinical/files/{private['file_id']}", headers=vera).status_code == 200
    export = client.get("/v1/subjects/p-1/export", headers=ADMIN).json()["clinical_files"]
    assert export["restricted_files_withheld"] == 1
