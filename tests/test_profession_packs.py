"""Profession packs (ADR 0014): schema, inheritance and the hard rules as inverted probes.

Each probe writes a pack that breaks a rule and checks that it does NOT load, so a
regression that weakens a validator turns a test red instead of reaching a patient."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from orchestrator import packs
from orchestrator.cli import main
from orchestrator.config import Settings
from orchestrator.packs import Pack, load_packs, pack_for, validate_config

REF = {"id": "R1", "instrument": "Some law", "article": "1", "status": "read"}
BASE: dict[str, Any] = {
    "id": "base",
    "name": "Base",
    "abstract": True,
    "jurisdiction": {"country": "EC", "refs": [REF]},
    "campaigns": {"banned_claims": ["cura"]},
    "documents": [
        {
            "id": "pnote",
            "title": "Psychotherapy note",
            "kind": "psychotherapy_note",
            "access": "author_only",
            "excluded_from": list(packs.PSYCHOTHERAPY_NOTE_EXCLUDED),
        },
        {
            "id": "diary",
            "title": "Diary",
            "kind": "patient_entry",
            "signature": "none",
            "access": "patient_and_treating",
            "excluded_from": list(packs.PATIENT_ENTRY_EXCLUDED),
        },
    ],
}


def write(directory: Path, *docs: dict[str, Any]) -> dict[str, Pack]:
    for doc in docs:
        (directory / f"{doc['id']}.yaml").write_text(yaml.safe_dump(doc), encoding="utf-8")
    load_packs.cache_clear()
    return load_packs(directory)


def child(**overrides: Any) -> dict[str, Any]:
    return {"id": "child", "name": "Child", "extends": "base", **overrides}


# --- the shipped packs ------------------------------------------------------------------


def test_shipped_packs_load_and_old_ones_keep_their_policy() -> None:
    shipped = load_packs()
    assert {"general", "dental", "retail", "ec-mental-health-base"} <= set(shipped)
    assert shipped["dental"].crm.recall_months == 6
    assert shipped["dental"].jurisdiction is None and shipped["dental"].documents == []


def test_mental_health_base_cites_only_declared_references() -> None:
    base = load_packs()["ec-mental-health-base"]
    assert base.jurisdiction is not None and base.jurisdiction.country == "EC"
    declared = {r.id for r in base.jurisdiction.refs}
    cited = {ref for doc in base.documents for ref in doc.legal_basis}
    assert cited <= declared
    # The unconfirmed items are visible, not hidden.
    assert {"RLOSM", "ETH-PSY", "TELEHEALTH"} <= set(packs.unverified_refs(base))


def test_mental_health_base_hard_rules_hold() -> None:
    base = load_packs()["ec-mental-health-base"]
    docs = {d.id: d for d in base.documents}
    assert docs["psychotherapy_note"].access == "author_only"
    assert set(docs["psychotherapy_note"].excluded_from) >= set(packs.PSYCHOTHERAPY_NOTE_EXCLUDED)
    assert docs["diary_entry"].access == "patient_and_treating"
    assert not base.profession.can_prescribe  # a psychologist refers, never prescribes
    assert base.safety.crisis.auto_contact_third_parties is False
    assert base.safety.minors.guardian_consent_required and base.safety.minors.minor_must_be_heard


def test_abstract_pack_cannot_serve_a_tenant(settings: Settings) -> None:
    settings.tenant_packs = {"acme": "ec-mental-health-base"}
    with pytest.raises(ValueError, match="abstract"):
        validate_config(settings)
    with pytest.raises(KeyError, match="abstract"):
        pack_for(settings, "acme")


# --- hard rules, inverted probes --------------------------------------------------------


def broken_document(index: int, **changes: Any) -> dict[str, Any]:
    doc = copy.deepcopy(BASE)
    doc["documents"][index].update(changes)
    return doc


@pytest.mark.parametrize(
    ("index", "changes", "message"),
    [
        (0, {"access": "care_team"}, "must be author_only"),
        (0, {"excluded_from": ["rag"]}, "must be excluded from"),
        (0, {"excluded_from": []}, "must be excluded from"),
        (1, {"access": "care_team"}, "must be patient_and_treating"),
        (1, {"signature": "professional"}, "not signed by a professional"),
        (1, {"excluded_from": ["rag", "memory"]}, "must be excluded from"),
    ],
)
def test_sensitive_document_kinds_cannot_be_loosened(
    tmp_path: Path, index: int, changes: dict[str, Any], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        write(tmp_path, broken_document(index, **changes))


def test_crisis_policy_cannot_enable_automatic_third_party_contact(tmp_path: Path) -> None:
    doc = {**BASE, "safety": {"crisis": {"auto_contact_third_parties": True}}}
    with pytest.raises(ValidationError, match="auto_contact_third_parties"):
        write(tmp_path, doc)


def test_a_profession_that_cannot_prescribe_cannot_carry_a_prescription(tmp_path: Path) -> None:
    rx = {"id": "rx", "title": "Prescription", "kind": "prescription"}
    with pytest.raises(ValidationError, match="cannot prescribe"):
        write(tmp_path, {**BASE, "documents": [rx]})


def test_controlled_worksheet_needs_the_controlled_permission(tmp_path: Path) -> None:
    ws = {"id": "ws", "title": "Worksheet", "kind": "controlled_prescription_worksheet"}
    ordinary = {"can_prescribe": True}
    with pytest.raises(ValidationError, match="controlled prescriptions"):
        write(tmp_path, {**BASE, "profession": ordinary, "documents": [ws]})
    allowed = {"can_prescribe": True, "can_use_controlled_prescription": True}
    assert "base" in write(tmp_path, {**BASE, "profession": allowed, "documents": [ws]})


def test_controlled_permission_requires_prescribing(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="requires can_prescribe"):
        write(tmp_path, {**BASE, "profession": {"can_use_controlled_prescription": True}})


def test_legal_basis_must_cite_a_declared_reference(tmp_path: Path) -> None:
    doc = copy.deepcopy(BASE)
    doc["documents"][0]["legal_basis"] = ["NOPE"]
    with pytest.raises(ValidationError, match="unknown references"):
        write(tmp_path, doc)


def test_typos_and_bad_values_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="Extra inputs"):
        write(tmp_path, {**BASE, "profesion": {"can_prescribe": True}})
    bad_ref = {**BASE, "jurisdiction": {"country": "EC", "refs": [{**REF, "status": "maybe"}]}}
    with pytest.raises(ValidationError, match="status"):
        write(tmp_path, bad_ref)
    with pytest.raises(ValidationError, match="country"):
        write(tmp_path, {**BASE, "jurisdiction": {"country": "ecuador"}})


def test_duplicate_ids_are_rejected(tmp_path: Path) -> None:
    doc = copy.deepcopy(BASE)
    doc["documents"].append(copy.deepcopy(doc["documents"][0]))
    with pytest.raises(ValidationError, match="duplicate document ids"):
        write(tmp_path, doc)
    refs = {"country": "EC", "refs": [REF, REF]}
    with pytest.raises(ValidationError, match="duplicate legal reference"):
        write(tmp_path, {**BASE, "jurisdiction": refs})


# --- inheritance ------------------------------------------------------------------------


def test_child_inherits_and_overrides(tmp_path: Path) -> None:
    loaded = write(tmp_path, BASE, child(profession={"can_prescribe": True}, description="Own"))
    kid = loaded["child"]
    assert kid.extends == "base" and kid.description == "Own"
    assert kid.profession.can_prescribe
    assert {d.id for d in kid.documents} == {"pnote", "diary"}  # inherited
    assert kid.jurisdiction is not None and kid.jurisdiction.refs[0].id == "R1"


def test_abstract_and_production_are_not_inherited(tmp_path: Path) -> None:
    loaded = write(tmp_path, {**BASE, "production": False}, child())
    assert loaded["base"].abstract and not loaded["child"].abstract
    prod = write(tmp_path, BASE, child(production=True))
    assert prod["child"].production and not prod["base"].production


def test_banned_claims_only_grow(tmp_path: Path) -> None:
    loaded = write(tmp_path, BASE, child(campaigns={"banned_claims": ["milagro"]}))
    assert loaded["child"].campaigns.banned_claims == ["cura", "milagro"]
    # Even an explicit empty list cannot lift the parent's prohibition.
    lifted = write(tmp_path, BASE, child(campaigns={"banned_claims": []}))
    assert lifted["child"].campaigns.banned_claims == ["cura"]


def test_a_child_cannot_drop_a_documents_exclusions(tmp_path: Path) -> None:
    # The child re-declares the note with fewer exclusions: the union keeps the parent's.
    weaker = {
        "id": "pnote",
        "title": "Psychotherapy note",
        "kind": "psychotherapy_note",
        "access": "author_only",
        "excluded_from": ["rag"],
    }
    loaded = write(tmp_path, BASE, child(documents=[weaker]))
    note = next(d for d in loaded["child"].documents if d.id == "pnote")
    assert set(note.excluded_from) >= set(packs.PSYCHOTHERAPY_NOTE_EXCLUDED)
    # And it cannot be turned into something readable by the whole care team.
    with pytest.raises(ValidationError, match="must be author_only"):
        write(tmp_path, BASE, child(documents=[{**weaker, "access": "care_team"}]))


def test_references_merge_by_id(tmp_path: Path) -> None:
    extra = {"id": "R2", "instrument": "Other", "status": "to_verify"}
    upgraded = {**REF, "note": "re-read"}
    loaded = write(tmp_path, BASE, child(jurisdiction={"refs": [extra, upgraded]}))
    refs = {r.id: r for r in loaded["child"].jurisdiction.refs}  # type: ignore[union-attr]
    assert set(refs) == {"R1", "R2"} and refs["R1"].note == "re-read"


def test_extends_errors(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="extends unknown pack 'ghost'"):
        write(tmp_path, child(extends="ghost"))
    a = {"id": "a", "name": "A", "extends": "b"}
    b = {"id": "b", "name": "B", "extends": "a"}
    with pytest.raises(ValueError, match="extends cycle"):
        write(tmp_path, a, b)


def test_file_name_must_match_the_id(tmp_path: Path) -> None:
    (tmp_path / "other.yaml").write_text(yaml.safe_dump(BASE), encoding="utf-8")
    load_packs.cache_clear()
    with pytest.raises(ValueError, match="must match the file name"):
        load_packs(tmp_path)
    (tmp_path / "other.yaml").write_text("- just\n- a list\n", encoding="utf-8")
    load_packs.cache_clear()
    with pytest.raises(ValueError, match="must be a mapping"):
        load_packs(tmp_path)


def test_abstract_pack_cannot_be_production(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="abstract pack cannot be production"):
        write(tmp_path, {**BASE, "production": True})


# --- strict mode and the CLI ------------------------------------------------------------


def test_strict_flags_production_packs_with_unverified_references(tmp_path: Path) -> None:
    shaky = {**REF, "id": "R9", "status": "to_verify"}
    jur = {"country": "EC", "refs": [shaky]}
    loaded = write(
        tmp_path,
        {"id": "live", "name": "Live", "production": True, "jurisdiction": jur},
        {"id": "draft", "name": "Draft", "jurisdiction": jur},
    )
    failures = packs.strict_failures(loaded)
    assert len(failures) == 1 and failures[0].startswith("live:")
    assert packs.unverified_refs(loaded["draft"]) == ["R9"]


def test_cli_pack_list_show_validate(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["pack", "list"]) == 0
    listing = capsys.readouterr().out
    assert "ec-mental-health-base" in listing and '"abstract": true' in listing

    assert main(["pack", "show", "ec-mental-health-base"]) == 0
    assert '"psychotherapy_note"' in capsys.readouterr().out

    assert main(["pack", "validate", "--strict"]) == 0  # nothing is marked production yet
    assert '"strict_failures": []' in capsys.readouterr().out

    with pytest.raises(SystemExit) as exc:
        main(["pack", "show", "nope"])
    assert exc.value.code == 2


def test_cli_pack_validate_fails_on_a_broken_pack(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def boom() -> dict[str, Pack]:
        raise ValueError("pack x: broken")

    monkeypatch.setattr(packs, "load_packs", boom)
    assert main(["pack", "validate"]) == 1
    assert "invalid pack: pack x: broken" in capsys.readouterr().err


def test_cli_strict_exits_nonzero_for_unverified_production(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    shaky = {**REF, "status": "to_verify"}
    live = Pack.model_validate(
        {
            "id": "live",
            "name": "Live",
            "production": True,
            "jurisdiction": {"country": "EC", "refs": [shaky]},
        }
    )
    monkeypatch.setattr(packs, "load_packs", lambda: {"live": live})
    assert main(["pack", "validate", "--strict"]) == 1
    assert "relies on unverified references" in capsys.readouterr().out


def test_mental_health_patients_get_their_own_consent_questions() -> None:
    # The generic pitch offers deals and profiles visit history: never for therapy
    # patients. Each question names its channel or scope and how to withdraw (LOPDP Art. 8).
    from orchestrator import packs

    for pack_id in ("ec-psychologist", "ec-psychiatrist"):
        prompts = packs.load_packs()[pack_id].consent_prompts
        text = " ".join(p.benefit + " " + p.detail for p in prompts.values()).lower()
        assert "oferta" not in text and "historial" not in text, pack_id
        assert "stop" in prompts["marketing"].detail.lower()
        assert "2 mensajes al mes" in prompts["marketing"].detail
        assert "historia clínica" in prompts["analytics"].detail
