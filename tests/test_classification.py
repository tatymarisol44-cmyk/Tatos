"""Data classification (audit 2026-10-08, item 2): every table and clinical document kind
has a class, the policy of each class is fixed, and the surface guard derives from it."""

from __future__ import annotations

import itertools
import json
from collections.abc import Iterator
from typing import get_args

import pytest
from fastapi.testclient import TestClient

import orchestrator.service  # noqa: F401  (registers every table on the metadata)
from orchestrator import packs
from orchestrator.api.app import create_app
from orchestrator.catalog import Catalog
from orchestrator.classification import (
    ALLOWED,
    DOCUMENT_CLASS,
    SINKS,
    TABLE_CLASS,
    DataClass,
    document_class,
    excluded,
)
from orchestrator.config import Settings
from orchestrator.db import metadata
from orchestrator.guardrails import cedula_ok, person_identifiers, redact_pii
from orchestrator.llm import FakeLLM
from orchestrator.packs import load_packs
from orchestrator.service import Orchestrator
from orchestrator.surfaces import HARD_EXCLUSIONS, SurfaceDenied, ensure_allowed

ADMIN = {"X-API-Key": "test-key"}


def _cedula(first_nine: str) -> str:
    total = 0
    for i, digit in enumerate(first_nine):
        product = int(digit) * (2 if i % 2 == 0 else 1)
        total += product - 9 if product > 9 else product
    return first_nine + str((10 - total % 10) % 10)


CEDULA = _cedula("171234567")  # synthetic, valid check digit


def test_every_table_has_a_class() -> None:
    """A new table fails here until someone decides how sensitive its rows are."""
    assert set(metadata.tables) == set(TABLE_CLASS)


def test_every_document_kind_has_a_class() -> None:
    assert set(get_args(packs.DocumentKind)) | {None} == set(DOCUMENT_CLASS)


def test_unknown_kind_fails_closed() -> None:
    assert document_class("a_kind_added_tomorrow") is DataClass.PSYCHOTHERAPY


def test_the_policy_itself() -> None:
    assert ALLOWED[DataClass.PSYCHOTHERAPY] == frozenset()
    assert ALLOWED[DataClass.CREDENTIAL] == frozenset()
    assert ALLOWED[DataClass.PATIENT_ENTRY] == {"audit_export"}
    # Health data never reaches retrieval, memory, campaigns or model reuse.
    assert not {"rag", "memory", "campaigns", "models"} & ALLOWED[DataClass.HEALTH]
    # Each class is at most as open as the one before it on the sensitivity scale.
    order = [DataClass.HEALTH, DataClass.PATIENT_ENTRY, DataClass.PSYCHOTHERAPY]
    for looser, stricter in itertools.pairwise(order):
        assert ALLOWED[stricter] <= ALLOWED[looser]
    assert set(ALLOWED) == set(DataClass)
    assert all(set(v) <= set(SINKS) for v in ALLOWED.values())


def test_pack_constants_come_from_the_policy() -> None:
    assert excluded(DataClass.PSYCHOTHERAPY) == packs.PSYCHOTHERAPY_NOTE_EXCLUDED
    assert excluded(DataClass.PATIENT_ENTRY) == packs.PATIENT_ENTRY_EXCLUDED
    assert set(packs.PSYCHOTHERAPY_NOTE_EXCLUDED) == set(SINKS)
    assert HARD_EXCLUSIONS["psychotherapy_note"] == excluded(DataClass.PSYCHOTHERAPY)
    assert HARD_EXCLUSIONS["record"] == excluded(DataClass.HEALTH)


@pytest.mark.parametrize("pack_id", sorted(load_packs()))
def test_every_shipped_document_respects_its_class(pack_id: str) -> None:
    pack = load_packs()[pack_id]
    for doc in pack.documents:
        for sink in excluded(document_class(doc.kind)):
            with pytest.raises(SurfaceDenied):
                ensure_allowed(pack, doc.kind, sink)


def test_cedula_check_digit() -> None:
    assert cedula_ok(CEDULA)
    assert not cedula_ok(CEDULA[:9] + str((int(CEDULA[9]) + 1) % 10))
    assert not cedula_ok("0991234567")  # a mobile number: third digit 9
    assert not cedula_ok("9912345678")  # no province 99
    assert person_identifiers(f"Paciente con CI {CEDULA}") == ["cedula"]
    # The clinic's own RUC (cédula + 001) and phone belong in its FAQ.
    assert person_identifiers(f"RUC {CEDULA}001, celular 0991234567") == []
    assert person_identifiers("tarjeta 4111 1111 1111 1111") == ["credit_card"]
    assert redact_pii(f"CI {CEDULA}") == ("CI [REDACTED_CEDULA]", ["cedula"])


@pytest.fixture
def client(settings: Settings, catalog: Catalog) -> Iterator[TestClient]:
    orch = Orchestrator(settings, catalog=catalog, llm=FakeLLM())
    with TestClient(create_app(settings, orch)) as c:
        yield c


def test_health_kinds_are_refused_from_the_knowledge_base(client: TestClient) -> None:
    for kind in ("record", "referral", "prescription", "psychotherapy_note", "patient_entry"):
        body = {"title": "x", "text": "Texto de prueba.", "kind": kind}
        response = client.post("/v1/knowledge/documents", json=body, headers=ADMIN)
        assert response.status_code == 403, kind
    assert client.get("/v1/knowledge/documents", headers=ADMIN).json() == []


def test_an_unlabelled_note_with_a_patient_id_is_refused(client: TestClient) -> None:
    body = {"title": "Sesion 3", "text": f"Paciente CI {CEDULA}: refiere insomnio."}
    response = client.post("/v1/knowledge/documents", json=body, headers=ADMIN)
    assert response.status_code == 422
    assert "cedula" in response.json()["detail"]
    assert client.get("/v1/knowledge/documents", headers=ADMIN).json() == []
    faq = {"title": "Contacto", "text": "Escríbenos al 0991234567. RUC 1712345675001."}
    assert client.post("/v1/knowledge/documents", json=faq, headers=ADMIN).status_code == 201


def test_logs_never_hold_personal_data(caplog: pytest.LogCaptureFixture) -> None:
    """Any logger, any handler, messages and tracebacks alike (telemetry.install_log_redaction
    runs on import)."""
    import logging

    import orchestrator.telemetry  # noqa: F401

    caplog.set_level(logging.INFO)
    for name in ("orchestrator.graph", "some.library", "httpx"):
        logging.getLogger(name).info("paciente ana@example.test CI %s tel +1 555 123 4567", CEDULA)
    try:
        raise ValueError(f"bad row for {CEDULA} via https://api.telegram.org/bot123:ABC-xyz/send")
    except ValueError:
        logging.getLogger("orchestrator.inbound").exception("failed")
    text = caplog.text + "".join(r.exc_text or "" for r in caplog.records)
    for secret in (CEDULA, "ana@example.test", "555 123 4567", "bot123:ABC-xyz"):
        assert secret not in text
    assert "[REDACTED_CEDULA]" in text and "bot<redacted>" in text


def test_the_model_never_receives_restricted_fields(settings: Settings, catalog: Catalog) -> None:
    """Patient chat is the approved workflow that sends the patient's OWN health data to a
    model (appointments, plans). Contact data, the birth date, the clinical record and any
    psychotherapy note must never be in a prompt, whoever is chatting."""
    llm = FakeLLM()
    orch = Orchestrator(settings, catalog=catalog, llm=llm)
    secrets = ["ana.secreta@example.test", "0991112233", "1990-02-03", "NotaPsicoterapiaX"]
    with TestClient(create_app(settings, orch)) as c:
        patient = {
            "id": "p-llm",
            "display_name": "Ana",
            "email": secrets[0],
            "phone": secrets[1],
            "birth_date": secrets[2],
        }
        assert c.post("/v1/crm/patients", json=patient, headers=ADMIN).status_code == 201
        staff = c.post(
            "/v1/admin/staff", json={"name": "dra.x", "roles": ["reviewer"]}, headers=ADMIN
        ).json()
        prof = {
            "professional_id": "dra.x",
            "display_name": "Dra X",
            "pack_id": "ec-psychologist",
            "staff_id": "dra.x",
        }
        assert c.post("/v1/admin/professionals", json=prof, headers=ADMIN).status_code == 201
        note = {"doc_type": "psychotherapy_note", "body": secrets[3]}
        clinician = {"X-API-Key": staff["key"]}
        written = c.post("/v1/clinical/patients/p-llm/documents", json=note, headers=clinician)
        assert written.status_code == 201
        key = c.post("/v1/crm/patients/p-llm/access", headers=ADMIN).json()["key"]
        me = {"X-API-Key": key}
        assert c.post("/v1/me/chat", json={"question": "¿Cuándo es mi cita?"}, headers=me)
        staff_chat = {"question": "Resume a la paciente", "subject_id": "p-llm"}
        c.post("/v1/chat", json=staff_chat, headers=clinician)
    prompts = json.dumps([[m for m in call] for call in llm.calls], default=str)
    assert llm.calls and "Ana" in prompts  # the patient's own name is fine
    for secret in secrets:
        assert secret not in prompts, secret
