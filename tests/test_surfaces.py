"""Mental-health packs and the surface guard (ADR 0014): a psychotherapy note or a diary
entry cannot reach shared retrieval, whatever the tenant's pack says or omits."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from orchestrator import packs
from orchestrator.api.app import create_app
from orchestrator.catalog import Catalog
from orchestrator.config import Settings
from orchestrator.llm import FakeLLM
from orchestrator.packs import Pack, load_packs
from orchestrator.service import Orchestrator
from orchestrator.surfaces import (
    HARD_EXCLUSIONS,
    SurfaceDenied,
    ensure_allowed,
    excluded_surfaces,
)

ACME = {"X-API-Key": "test-key"}  # tenant "acme", mapped to the psychologist pack below
GLOBEX = {"X-API-Key": "other-key"}  # tenant "globex", on the general pack
TEXT = "Weekly routine of the practice, opening hours and how to book an appointment."


# --- the shipped mental-health packs ----------------------------------------------------


def test_psychologist_refers_and_never_prescribes() -> None:
    pack = load_packs()["ec-psychologist"]
    assert pack.extends == "ec-mental-health-base" and not pack.abstract
    assert not pack.profession.can_prescribe
    assert not any(
        d.kind in ("prescription", "controlled_prescription_worksheet") for d in pack.documents
    )
    ids = {d.id for d in pack.documents}
    assert {"consent_therapy", "psychotherapy_note", "diary_entry", "referral"} <= ids  # inherited
    assert {"consent_online", "attendance_certificate", "psychological_report"} <= ids  # own


def test_psychiatrist_may_prescribe_but_only_drafts_the_controlled_form() -> None:
    pack = load_packs()["ec-psychiatrist"]
    assert pack.profession.can_prescribe and pack.profession.can_use_controlled_prescription
    docs = {d.id: d for d in pack.documents}
    worksheet = docs["controlled_worksheet"]
    assert worksheet.kind == "controlled_prescription_worksheet"  # a worksheet, not the legal form
    assert "ACESS" in worksheet.note and "not the legal document" in worksheet.note.lower()
    assert set(worksheet.legal_basis) == {"ACESS-0046-4-6", "ACESS-0046-25", "ACESS-0046-27"}
    assert docs["psychotherapy_note"].access == "author_only"  # still inherited, still locked


@pytest.mark.parametrize("pack_id", ["ec-psychologist", "ec-psychiatrist"])
def test_clinical_prohibitions_survive_inheritance(pack_id: str) -> None:
    pack = load_packs()[pack_id]
    note = next(d for d in pack.documents if d.kind == "psychotherapy_note")
    entry = next(d for d in pack.documents if d.kind == "patient_entry")
    assert set(note.excluded_from) >= set(packs.PSYCHOTHERAPY_NOTE_EXCLUDED)
    assert set(entry.excluded_from) >= set(packs.PATIENT_ENTRY_EXCLUDED)
    assert pack.safety.crisis.auto_contact_third_parties is False
    assert "curación" in pack.campaigns.banned_claims  # the base's ban is still there


def test_both_packs_are_servable_and_report_their_open_references() -> None:
    shipped = load_packs()
    for pack_id in ("ec-psychologist", "ec-psychiatrist"):
        assert not shipped[pack_id].abstract and not shipped[pack_id].production
    # Honest about what is not confirmed: the psychiatrist's own references are all read or
    # secondary, but it inherits the base's open items.
    assert "TELEHEALTH" in packs.unverified_refs(shipped["ec-psychiatrist"])
    assert "REG-PSY" not in packs.unverified_refs(shipped["ec-psychologist"])  # secondary, not open


# --- the guard --------------------------------------------------------------------------


def test_hard_exclusions_apply_even_to_a_pack_with_no_documents() -> None:
    general = load_packs()["general"]
    assert general.documents == []
    for kind, surfaces in HARD_EXCLUSIONS.items():
        for surface in surfaces:
            with pytest.raises(SurfaceDenied, match=f"a {kind} may not enter {surface}"):
                ensure_allowed(general, kind, surface)


def test_ordinary_content_and_other_kinds_are_not_blocked() -> None:
    general = load_packs()["general"]
    ensure_allowed(general, None, "rag")  # an FAQ
    ensure_allowed(general, "consent", "rag")
    ensure_allowed(general, "referral", "campaigns")
    # A psychotherapy note is not shut out of surfaces it was never listed for.
    assert "audit_export" in excluded_surfaces(general, "psychotherapy_note")
    assert "audit_export" not in excluded_surfaces(general, "patient_entry")


def test_a_pack_can_shut_a_kind_out_of_more_surfaces() -> None:
    strict = Pack.model_validate(
        {
            "id": "strict",
            "name": "Strict",
            "documents": [
                {"id": "c", "title": "Consent", "kind": "consent", "excluded_from": ["rag"]}
            ],
        }
    )
    with pytest.raises(SurfaceDenied):
        ensure_allowed(strict, "consent", "rag")
    ensure_allowed(strict, "consent", "campaigns")


# --- the HTTP route ---------------------------------------------------------------------


@pytest.fixture
def client(settings: Settings, catalog: Catalog) -> Iterator[TestClient]:
    settings.tenant_packs = {"acme": "ec-psychologist"}
    orch = Orchestrator(settings, catalog=catalog, llm=FakeLLM())
    with TestClient(create_app(settings, orch)) as c:
        yield c


def upload(client: TestClient, headers: dict[str, str], **fields: str) -> int:
    body = {"title": "Practice info", "text": TEXT, **fields}
    return client.post("/v1/knowledge/documents", json=body, headers=headers).status_code


@pytest.mark.parametrize("kind", ["psychotherapy_note", "patient_entry"])
@pytest.mark.parametrize("headers", [ACME, GLOBEX], ids=["psychologist-pack", "general-pack"])
def test_protected_kinds_are_refused_for_every_tenant_and_leave_no_trace(
    client: TestClient, headers: dict[str, str], kind: str
) -> None:
    assert upload(client, headers, kind=kind, doc_id="secret-note") == 403
    assert client.get("/v1/knowledge/documents", headers=headers).json() == []
    hits = client.post("/v1/knowledge/search", json={"query": "practice"}, headers=headers).json()
    assert hits == []


def test_ordinary_documents_still_upload(client: TestClient) -> None:
    assert upload(client, ACME) == 201  # no kind: ordinary company content
    assert upload(client, ACME, kind="consent", doc_id="consent-template") == 201
    assert upload(client, ACME, kind="not-a-kind") == 422  # unknown kinds are rejected too
