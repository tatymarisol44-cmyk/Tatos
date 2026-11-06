"""Author-private source drafts. Extraction never authorizes a clinical instrument.

OOXML is parsed locally as bounded inert XML; no office process, network, formulas,
macros or model calls. PDF/images remain attachments for the existing reviewed
clinical workflow. No generic external-URL fetcher or invented API adapter exists.
"""

from __future__ import annotations

import hashlib
import io
import json
import uuid
import zipfile
import zlib
from pathlib import PurePosixPath
from typing import Any, Literal
from xml.etree import ElementTree as ET

from pydantic import BaseModel, ConfigDict, Field, StrictBool, ValidationError, field_validator
from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    Index,
    Integer,
    LargeBinary,
    String,
    Table,
    insert,
    select,
    update,
)
from sqlalchemy.exc import IntegrityError

from orchestrator.auth import Principal
from orchestrator.clinical_files import safe_name
from orchestrator.db import Database, aware, metadata, utcnow
from orchestrator.governance import AuditLog
from orchestrator.instruments import Instruments, InstrumentSpec

MAX_BYTES = 5 * 1024 * 1024
MAX_TEXT = 30000
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
S = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"

instrument_imports = Table(
    "instrument_imports",
    metadata,
    Column("tenant", String(64), primary_key=True),
    Column("id", String(32), primary_key=True),
    Column("author_id", String(128), nullable=False),
    Column("revision", Integer, nullable=False),
    Column("manifest", JSON, nullable=False),
    Column("filename", String(200), nullable=False),
    Column("sha256", String(64), nullable=False),
    Column("format", String(20), nullable=False),
    Column("preview", JSON, nullable=False),
    Column("content", LargeBinary, nullable=True),
    Column("state", String(24), nullable=False),
    Column("instrument_id", String(40), nullable=True),
    Column("review", JSON, nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Index("ix_instrument_imports_author", "tenant", "author_id", "created_at"),
)


class ImportRejected(ValueError):
    pass


class SourceManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=120)
    publisher: str = Field(min_length=1, max_length=200)
    licence: str = Field(min_length=1, max_length=500)
    edition: str = Field(min_length=1, max_length=100)
    source_reference: str = Field(min_length=1, max_length=500)
    permitted_use: str = Field(min_length=1, max_length=500)
    authorised_api: str = Field(default="", max_length=500)
    calculation_reference: str = Field(default="", max_length=500)
    clinical_validation_reference: str = Field(default="", max_length=500)
    # References are inert bibliographic text, never fetched or accepted as credentials.


class ImportReview(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    revision: int = Field(ge=1)
    spec: InstrumentSpec
    visibility: Literal["private", "establishment"] = "private"
    licensed: StrictBool
    reviewed: StrictBool
    no_patient_data: StrictBool
    calculation_reference: str = Field(min_length=1, max_length=500)
    validation_reference: str = Field(min_length=1, max_length=500)

    @field_validator("licensed", "reviewed", "no_patient_data")
    @classmethod
    def confirmed(cls, value: bool) -> bool:
        if value is not True:
            raise ValueError("explicit confirmation is required")
        return value


def _xml(data: bytes) -> ET.Element:
    upper = data.upper()
    if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper or b"\x00" in data:
        raise ImportRejected("DTD, entities and NUL-containing XML are not accepted")
    try:
        # DTD/entities and UTF-16/32 were rejected above; each XML is bounded to 2 MB.
        root = ET.fromstring(data)  # noqa: S314 -- guarded inert OOXML, no external resolver
    except ET.ParseError as exc:
        raise ImportRejected("invalid XML document") from exc
    if sum(1 for _ in root.iter()) > 50000:
        raise ImportRejected("XML has too many nodes")
    return root


def _office(content: bytes) -> dict[str, bytes]:
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            infos = archive.infolist()
            if len(infos) > 200 or sum(i.file_size for i in infos) > 10 * 1024 * 1024:
                raise ImportRejected("office archive exceeds the extraction limit")
            files: dict[str, bytes] = {}
            for info in infos:
                name = info.filename
                path = PurePosixPath(name)
                if (
                    path.is_absolute()
                    or ".." in path.parts
                    or "\\" in name
                    or ":" in name
                    or name in files
                    or info.flag_bits & 1
                    or info.file_size > 2 * 1024 * 1024
                    or info.file_size > max(1, info.compress_size) * 100
                    or any(
                        x in name.lower()
                        for x in ("vbaproject", "activex", "embeddings/", "externallinks/")
                    )
                ):
                    raise ImportRejected("unsafe or oversized office member")
                if info.is_dir():
                    continue
                data = archive.read(info)
                if name.endswith((".xml", ".rels")):
                    root = _xml(data)
                    if any(
                        n.attrib.get("TargetMode", "").lower() == "external" for n in root.iter()
                    ):
                        raise ImportRejected("external office links are not accepted")
                    if any(
                        "macroenabled" in str(v).lower()
                        for n in root.iter()
                        for v in n.attrib.values()
                    ):
                        raise ImportRejected("macro-enabled office files are not accepted")
                files[name] = data
            return files
    except (zipfile.BadZipFile, RuntimeError, NotImplementedError, zlib.error, EOFError) as exc:
        raise ImportRejected("invalid or unsupported office archive") from exc


def extract_source(content: bytes) -> tuple[str, dict[str, Any]]:
    """Return an inert preview, never inferred scoring or diagnostic conclusions."""
    if not content or len(content) > MAX_BYTES:
        raise ImportRejected("upload a non-empty file of at most 5 MB")
    if content.startswith((b"%PDF-", b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff")):
        kind = "pdf" if content.startswith(b"%PDF-") else "image"
        return kind, {
            "text": "",
            "extraction": "attachment_only",
            "notice": (
                "Use clinical files or reviewed test images; no PDF OCR or scoring was performed."
            ),
        }
    if content.startswith(b"PK"):
        files = _office(content)
        if "[Content_Types].xml" not in files:
            raise ImportRejected("not an OOXML document")
        if "word/document.xml" in files:
            if "xl/workbook.xml" in files:
                raise ImportRejected("ambiguous office file")
            root = _xml(files["word/document.xml"])
            text = "\n".join("".join(n.itertext()) for n in root.iter(W + "t"))
            kind = "docx"
        elif "xl/workbook.xml" in files:
            shared: list[str] = []
            if "xl/sharedStrings.xml" in files:
                shared = [
                    "".join(n.itertext())
                    for n in _xml(files["xl/sharedStrings.xml"]).iter(S + "si")
                ]
            cells: list[str] = []
            text_size = 0
            for name, data in sorted(files.items()):
                if not name.startswith("xl/worksheets/") or not name.endswith(".xml"):
                    continue
                root = _xml(data)
                if any(True for _ in root.iter(S + "f")):
                    raise ImportRejected(
                        "remove spreadsheet formulas; only literal cells are accepted"
                    )
                for cell in root.iter(S + "c"):
                    value = cell.find(S + "v")
                    raw = value.text or "" if value is not None else ""
                    if cell.get("t") == "s":
                        try:
                            index = int(raw)
                            if index < 0:
                                raise ValueError("negative shared-string index")
                            raw = shared[index]
                        except (ValueError, IndexError) as exc:
                            raise ImportRejected("invalid shared-string cell") from exc
                    elif cell.get("t") == "inlineStr":
                        raw = "".join(n.text or "" for n in cell.iter(S + "t"))
                    # Shared strings can be referenced thousands of times: bound
                    # output before materializing each copy, not after joining them.
                    if len(raw) > MAX_TEXT:
                        raise ImportRejected("extracted text exceeds 30000 characters")
                    line = f"{PurePosixPath(name).name}:{cell.get('r', '')}\t{raw}"
                    text_size += len(line) + 1
                    if text_size > MAX_TEXT:
                        raise ImportRejected("extracted text exceeds 30000 characters")
                    cells.append(line)
                    if len(cells) > 2000:
                        raise ImportRejected("at most 2000 cells can be imported")
            text, kind = "\n".join(cells), "xlsx"
        else:
            raise ImportRejected("only DOCX and XLSX office documents are accepted")
        if len(text) > MAX_TEXT:
            raise ImportRejected("extracted text exceeds 30000 characters")
        return kind, {
            "text": text,
            "extraction": "local_literal_text",
            "notice": "Review wording, licence and scoring against the authorised edition.",
        }
    try:
        payload = json.loads(content.decode("utf-8-sig"))
        spec = InstrumentSpec.model_validate(payload)
    except (UnicodeDecodeError, ValueError, ValidationError, RecursionError) as exc:
        raise ImportRejected(
            "expected an authorised instrument JSON, DOCX, XLSX, PDF or image"
        ) from exc
    return "json", {
        "suggested_spec": spec.model_dump(mode="json"),
        "text": "",
        "extraction": "validated_definition_shape",
        "notice": (
            "Schema validity does not prove permission, scoring authorisation or clinical validity."
        ),
    }


class InstrumentImports:
    def __init__(self, db: Database, audit: AuditLog, instruments: Instruments) -> None:
        self.db, self.audit, self.instruments = db, audit, instruments

    @staticmethod
    def visible(p: Principal) -> Any:
        return (instrument_imports.c.tenant == p.tenant) & (instrument_imports.c.author_id == p.id)

    @staticmethod
    def public(row: Any, detail: bool = False) -> dict[str, Any]:
        names = (
            "id",
            "revision",
            "manifest",
            "filename",
            "sha256",
            "format",
            "state",
            "instrument_id",
            "review",
        )
        result = {n: row[n] for n in names}
        result["created_at"] = aware(row["created_at"])
        if detail:
            result["preview"] = row["preview"]
        result["api_state"] = (
            "blocked_pending_authorised_provider_contract_and_adapter"
            if row["manifest"].get("authorised_api")
            else "not_declared"
        )
        return result

    async def listing(self, p: Principal) -> list[dict[str, Any]]:
        async with self.db.engine.connect() as conn:
            rows = (
                (
                    await conn.execute(
                        select(
                            *[
                                c
                                for c in instrument_imports.c
                                if c.name not in ("content", "preview")
                            ]
                        )
                        .where(self.visible(p))
                        .order_by(instrument_imports.c.created_at.desc())
                        .limit(100)
                    )
                )
                .mappings()
                .all()
            )
        return [self.public(r) for r in rows]

    async def get(self, p: Principal, identifier: str) -> dict[str, Any]:
        async with self.db.engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        select(instrument_imports).where(
                            self.visible(p), instrument_imports.c.id == identifier
                        )
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                raise KeyError(identifier)
        await self.audit.record(
            p.tenant, p.id, "instrument.import.read", f"instrument_import/{identifier}"
        )
        return self.public(row, True)

    async def upload(
        self, p: Principal, manifest: SourceManifest, filename: str, content: bytes
    ) -> dict[str, Any]:
        # CPU work is bounded and dispatched outside the event loop.
        import asyncio

        kind, preview = await asyncio.to_thread(extract_source, content)
        identifier = uuid.uuid4().hex[:16]
        async with self.db.engine.begin() as conn:
            await conn.execute(
                insert(instrument_imports).values(
                    tenant=p.tenant,
                    id=identifier,
                    author_id=p.id,
                    revision=1,
                    manifest=manifest.model_dump(),
                    filename=safe_name(filename),
                    sha256=hashlib.sha256(content).hexdigest(),
                    format=kind,
                    preview=preview,
                    content=content,
                    state="draft",
                    created_at=utcnow(),
                )
            )
            await self.audit.record_in(
                conn,
                p.tenant,
                p.id,
                "instrument.import.uploaded",
                f"instrument_import/{identifier}",
                details={"format": kind, "sha256": hashlib.sha256(content).hexdigest()},
            )
        return await self.get(p, identifier)

    async def promote(self, p: Principal, identifier: str, review: ImportReview) -> dict[str, Any]:
        if not (
            review.licensed is True and review.reviewed is True and review.no_patient_data is True
        ):
            raise ImportRejected(
                "licence, professional review and absence of patient data must be confirmed"
            )
        try:
            async with self.db.engine.begin() as conn:
                row = (
                    (
                        await conn.execute(
                            select(instrument_imports)
                            .where(self.visible(p), instrument_imports.c.id == identifier)
                            .with_for_update()
                        )
                    )
                    .mappings()
                    .first()
                )
                if row is None:
                    raise KeyError(identifier)
                fingerprint = hashlib.sha256(review.model_dump_json().encode()).hexdigest()
                if row["state"] == "incorporated":
                    if row["review"]["sha256"] != fingerprint:
                        raise ImportRejected("this draft was incorporated with a different review")
                    return {
                        "instrument_id": row["instrument_id"],
                        "version": 1,
                        "state": "incorporated",
                    }
                if row["revision"] != review.revision or row["state"] != "draft":
                    raise ImportRejected("reload the source draft before reviewing")
                instrument_id = "import-" + identifier
                await self.instruments.write_in(
                    conn,
                    p,
                    instrument_id,
                    1,
                    review.spec,
                    review.visibility,
                    "instrument.import.incorporated",
                )
                await conn.execute(
                    update(instrument_imports)
                    .where(self.visible(p), instrument_imports.c.id == identifier)
                    .values(
                        state="incorporated",
                        revision=review.revision + 1,
                        instrument_id=instrument_id,
                        review={
                            "sha256": fingerprint,
                            "calculation_reference": review.calculation_reference,
                            "validation_reference": review.validation_reference,
                            "reviewed_by": p.id,
                            "reviewed_at": utcnow().isoformat(),
                        },
                    )
                )
                await self.audit.record_in(
                    conn,
                    p.tenant,
                    p.id,
                    "instrument.import.reviewed",
                    f"instrument_import/{identifier}",
                    details={
                        "source_sha256": row["sha256"],
                        "instrument_id": instrument_id,
                        "version": 1,
                    },
                )
        except IntegrityError as exc:
            raise ImportRejected(
                "another request incorporated this source; reload before retrying"
            ) from exc
        return {"instrument_id": instrument_id, "version": 1, "state": "incorporated"}

    async def purge(self, p: Principal, identifier: str, revision: int) -> None:
        async with self.db.engine.begin() as conn:
            row = (
                await conn.execute(
                    select(instrument_imports.c.revision, instrument_imports.c.state)
                    .where(self.visible(p), instrument_imports.c.id == identifier)
                    .with_for_update()
                )
            ).first()
            if row is None:
                raise KeyError(identifier)
            if row.revision != revision:
                raise ImportRejected("source changed; reload before purging")
            await conn.execute(
                update(instrument_imports)
                .where(self.visible(p), instrument_imports.c.id == identifier)
                .values(
                    content=None,
                    preview={},
                    revision=revision + 1,
                    state="purged" if row.state == "draft" else row.state,
                )
            )
            await self.audit.record_in(
                conn,
                p.tenant,
                p.id,
                "instrument.import.source_purged",
                f"instrument_import/{identifier}",
            )
