"""Synthetic source imports: inert parsing, licence review, atomic reuse, isolation."""

import asyncio
import io
import json
import zipfile
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from orchestrator.auth import Principal
from orchestrator.instrument_imports import (
    ImportRejected,
    ImportReview,
    SourceManifest,
    extract_source,
    instrument_imports,
)
from orchestrator.instruments import InstrumentSpec
from orchestrator.service import Orchestrator
from tests.test_instruments import CUSTOM

MANIFEST = {
    "name": "Test sintético sin uso clínico",
    "publisher": "Autora ficticia",
    "licence": "Material inventado para pruebas",
    "edition": "1 de demostración",
    "source_reference": "fixture local de QA",
    "permitted_use": "Pruebas privadas exclusivamente",
}


def office(files: dict[str, str]) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        for name, content in {"[Content_Types].xml": "<Types/>", **files}.items():
            archive.writestr(name, content)
    return out.getvalue()


def test_extracts_only_literal_docx_and_xlsx_text() -> None:
    word = office(
        {
            "word/document.xml": (
                '<w:document xmlns:w="http://schemas.openxmlformats.org/'
                'wordprocessingml/2006/main"><w:t>Pregunta sintética</w:t></w:document>'
            )
        }
    )
    kind, preview = extract_source(word)
    assert kind == "docx" and preview["text"] == "Pregunta sintética"
    excel = office(
        {
            "xl/workbook.xml": "<workbook/>",
            "xl/worksheets/sheet1.xml": (
                '<worksheet xmlns="http://schemas.openxmlformats.org/'
                'spreadsheetml/2006/main"><c r="A1" t="inlineStr"><is><t>Pregunta</t>'
                '</is></c><c r="B1"><v>3</v></c></worksheet>'
            ),
        }
    )
    kind, preview = extract_source(excel)
    assert kind == "xlsx" and "A1\tPregunta" in preview["text"] and "B1\t3" in preview["text"]
    assert "scoring" not in preview


@pytest.mark.parametrize(
    "files",
    [
        {"../outside.xml": "<x/>", "word/document.xml": "<doc/>"},
        {"word/document.xml": '<!DOCTYPE doc [<!ENTITY e "secret">]><doc>&e;</doc>'},
        {
            "word/document.xml": "<doc/>",
            "word/_rels/document.xml.rels": '<Relationships><Relationship TargetMode="External" Target="https://example.invalid"/></Relationships>',
        },
        {"word/document.xml": "<doc/>", "word/vbaProject.bin": "macro"},
        {
            "xl/workbook.xml": "<workbook/>",
            "xl/worksheets/sheet1.xml": '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><c><f>WEBSERVICE()</f><v>0</v></c></worksheet>',
        },
        {"word/document.xml": "<bad>"},
    ],
)
def test_rejects_archive_paths_entities_links_macros_formulas_and_malformed_xml(
    files: dict[str, str],
) -> None:
    with pytest.raises(ImportRejected):
        extract_source(office(files))


def test_source_shape_and_size_are_not_clinical_validation() -> None:
    with pytest.raises(ImportRejected):
        extract_source(b"not a test")
    with pytest.raises(ImportRejected):
        extract_source(b" " * (5 * 1024 * 1024 + 1))
    kind, preview = extract_source(json.dumps(CUSTOM).encode())
    assert kind == "json" and preview["suggested_spec"]["name"] == CUSTOM["name"]
    assert "does not prove" in preview["notice"]
    for content in (b"%PDF-1.7 synthetic", b"\x89PNG\r\n\x1a\nsynthetic"):
        _, preview = extract_source(content)
        assert preview["extraction"] == "attachment_only" and "suggested_spec" not in preview
    for bad in (False, 1, "true"):
        with pytest.raises(ValidationError):
            ImportReview(
                revision=1,
                spec=CUSTOM,
                licensed=bad,
                reviewed=True,
                no_patient_data=True,
                calculation_reference="synthetic",
                validation_reference="not clinically validated",
            )


def test_shared_strings_cannot_expand_into_unbounded_output() -> None:
    # Small archive, large repetition: reject during extraction, before joining.
    document = office(
        {
            "xl/workbook.xml": "<workbook/>",
            "xl/sharedStrings.xml": '<sst xmlns="http://schemas.openxmlformats.org/'
            'spreadsheetml/2006/main"><si><t>' + "x" * 20000 + "</t></si></sst>",
            "xl/worksheets/sheet1.xml": '<worksheet xmlns="http://schemas.openxmlformats.org/'
            'spreadsheetml/2006/main">' + '<c t="s"><v>0</v></c>' * 10 + "</worksheet>",
        }
    )
    with pytest.raises(ImportRejected, match="30000"):
        extract_source(document)


def review() -> ImportReview:
    return ImportReview(
        revision=1,
        spec=CUSTOM,
        licensed=True,
        reviewed=True,
        no_patient_data=True,
        calculation_reference="invented QA arithmetic",
        validation_reference="synthetic: not clinically validated",
    )


async def test_author_private_promotion_is_idempotent_and_uses_existing_instruments(
    orchestrator: Orchestrator,
) -> None:
    p = Principal("acme", "clinician", "staff", frozenset({"reviewer"}))
    service = orchestrator.instrument_imports
    draft = await service.upload(
        p, SourceManifest.model_validate(MANIFEST), "test.json", json.dumps(CUSTOM).encode()
    )
    other = Principal("acme", "another-clinician", "staff", frozenset({"reviewer"}))
    foreign = Principal("globex", "clinician", "staff", frozenset({"reviewer"}))
    for stranger in (other, foreign):
        assert await service.listing(stranger) == []
        with pytest.raises(KeyError):
            await service.promote(stranger, draft["id"], review())
    responses = await asyncio.gather(
        service.promote(p, draft["id"], review()), service.promote(p, draft["id"], review())
    )
    assert responses[0]["instrument_id"] == responses[1]["instrument_id"]
    made = await orchestrator.instruments.get(p, responses[0]["instrument_id"])
    assert made["spec"] == InstrumentSpec.model_validate(CUSTOM).model_dump(mode="json")
    assert len(await orchestrator.instruments.available(p)) == 1
    current = await service.get(p, draft["id"])
    assert current["review"]["calculation_reference"] == "invented QA arithmetic"
    await service.purge(p, draft["id"], current["revision"])
    async with orchestrator.db.engine.connect() as conn:
        assert (
            await conn.execute(
                select(instrument_imports.c.content).where(instrument_imports.c.id == draft["id"])
            )
        ).scalar_one() is None
    assert (await service.get(p, draft["id"]))["sha256"] == draft["sha256"]


async def test_failed_audit_rolls_back_both_import_and_definition(
    orchestrator: Orchestrator, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = Principal("acme", "clinician", "staff", frozenset({"reviewer"}))
    service = orchestrator.instrument_imports
    draft = await service.upload(
        p, SourceManifest.model_validate(MANIFEST), "test.json", json.dumps(CUSTOM).encode()
    )
    original = orchestrator.audit.record_in

    async def fail(*args: Any, **kwargs: Any) -> None:
        if args[3] == "instrument.import.reviewed":
            raise RuntimeError("synthetic audit failure")
        await original(*args, **kwargs)

    monkeypatch.setattr(orchestrator.audit, "record_in", fail)
    with pytest.raises(RuntimeError):
        await service.promote(p, draft["id"], review())
    assert await orchestrator.instruments.available(p) == []
    assert (await service.get(p, draft["id"]))["state"] == "draft"
