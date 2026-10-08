"""Clinical files: a signed consent (PDF), an external report, a scanned test, an image.

Stored in the database, next to the clinical record: same tenant isolation, encryption
at rest (Cloud SQL) and point-in-time backups, no separate bucket to secure. Up to
`CLINICAL_FILE_MAX_BYTES` (10 MB) each.

- **Only what it says it is.** PDF, PNG, JPEG or plain text, checked by the content's
  signature (magic bytes), not by the file name or the browser's declared type.
- **Integrity.** SHA-256 of the content at upload: what is downloaded later can be
  proven identical (e.g. a consent signed outside the system, decision of 2026-10-07).
- **Access.** Clinicians only; `author_only` files (a psychotherapy-related document)
  are visible to their author alone. Every upload and every download is audited.
- **Data class.** Like the clinical record (classification: PSYCHOTHERAPY at most): never
  sent to retrieval, models, campaigns or analytics.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from typing import Any, Literal

from sqlalchemy import (
    Column,
    DateTime,
    Integer,
    LargeBinary,
    String,
    Table,
    and_,
    insert,
    or_,
    select,
)

from orchestrator.auth import Principal, Role
from orchestrator.crm import patients
from orchestrator.db import Database, metadata, utcnow
from orchestrator.governance import AuditLog

Access = Literal["care_team", "author_only"]
# Content signatures of the accepted types.
SIGNATURES: dict[str, tuple[bytes, ...]] = {
    "application/pdf": (b"%PDF-",),
    "image/png": (b"\x89PNG\r\n\x1a\n",),
    "image/jpeg": (b"\xff\xd8\xff",),
}


class FileRejected(ValueError):
    """Wrong type, empty, too large, or not what it claims to be."""


def sniff(content: bytes) -> str:
    """The media type proven by the content itself; plain UTF-8 text without control
    characters is accepted as text/plain."""
    for media_type, prefixes in SIGNATURES.items():
        if content.startswith(prefixes):
            return media_type
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise FileRejected("only PDF, PNG, JPEG or plain text files are accepted") from exc
    if re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", text):
        raise FileRejected("only PDF, PNG, JPEG or plain text files are accepted")
    return "text/plain"


def safe_name(filename: str) -> str:
    name = re.sub(r"[^\w.\- ]+", "_", filename.rsplit("/", 1)[-1].rsplit("\\", 1)[-1])
    return name.strip(" .")[:200] or "file"


clinical_files = Table(
    "clinical_files",
    metadata,
    Column("tenant", String(64), primary_key=True),
    Column("file_id", String(32), primary_key=True),
    Column("patient_id", String(64), nullable=False, index=True),
    Column("label", String(120), nullable=False),
    Column("filename", String(200), nullable=False),
    Column("media_type", String(40), nullable=False),
    Column("size", Integer, nullable=False),
    Column("sha256", String(64), nullable=False),
    Column("access", String(24), nullable=False),
    Column("author_id", String(128), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("content", LargeBinary, nullable=False),
)

METADATA = [c for c in clinical_files.c if c.name not in ("tenant", "content")]


def _public(row: Any) -> dict[str, Any]:
    out = {c.name: getattr(row, c.name) for c in METADATA}
    out["created_at"] = out["created_at"].isoformat() if out["created_at"] else None
    return out


class ClinicalFiles:
    def __init__(self, db: Database, audit: AuditLog, max_bytes: int) -> None:
        self.db = db
        self.audit = audit
        self.max_bytes = max_bytes

    @staticmethod
    def _visible(principal: Principal) -> Any:
        own = and_(
            clinical_files.c.access == "author_only", clinical_files.c.author_id == principal.id
        )
        if principal.has(Role.REVIEWER):
            return or_(clinical_files.c.access != "author_only", own)
        return own

    async def _require_patient(self, tenant: str, patient_id: str) -> None:
        query = select(patients.c.id).where(
            and_(patients.c.tenant == tenant, patients.c.id == patient_id)
        )
        async with self.db.engine.connect() as conn:
            if (await conn.execute(query)).first() is None:
                raise KeyError(patient_id)

    async def upload(
        self,
        principal: Principal,
        patient_id: str,
        *,
        filename: str,
        content: bytes,
        label: str,
        access: Access = "care_team",
    ) -> dict[str, Any]:
        await self._require_patient(principal.tenant, patient_id)
        if not content:
            raise FileRejected("the file is empty")
        if len(content) > self.max_bytes:
            raise FileRejected(f"the file is larger than {self.max_bytes // (1024 * 1024)} MB")
        media_type = sniff(content)
        file_id = uuid.uuid4().hex[:16]
        digest = hashlib.sha256(content).hexdigest()
        async with self.db.engine.begin() as conn:
            await conn.execute(
                insert(clinical_files).values(
                    tenant=principal.tenant,
                    file_id=file_id,
                    patient_id=patient_id,
                    label=label,
                    filename=safe_name(filename),
                    media_type=media_type,
                    size=len(content),
                    sha256=digest,
                    access=access,
                    author_id=principal.id,
                    created_at=utcnow(),
                    content=content,
                )
            )
            await self.audit.record_in(
                conn,
                principal.tenant,
                principal.id,
                "clinical_file.uploaded",
                f"clinical_file/{file_id}",
                subject_id=patient_id,
                details={"media_type": media_type, "size": len(content), "sha256": digest},
            )
        return await self.info(principal, file_id)

    async def info(self, principal: Principal, file_id: str) -> dict[str, Any]:
        query = select(*METADATA).where(
            and_(
                clinical_files.c.tenant == principal.tenant,
                clinical_files.c.file_id == file_id,
                self._visible(principal),
            )
        )
        async with self.db.engine.connect() as conn:
            row = (await conn.execute(query)).first()
        if row is None:  # another author's restricted file looks exactly like a missing one
            raise KeyError(file_id)
        return _public(row)

    async def for_patient(self, principal: Principal, patient_id: str) -> list[dict[str, Any]]:
        await self._require_patient(principal.tenant, patient_id)
        query = (
            select(*METADATA)
            .where(
                and_(
                    clinical_files.c.tenant == principal.tenant,
                    clinical_files.c.patient_id == patient_id,
                    self._visible(principal),
                )
            )
            .order_by(clinical_files.c.created_at)
        )
        async with self.db.engine.connect() as conn:
            return [_public(r) for r in await conn.execute(query)]

    async def download(self, principal: Principal, file_id: str) -> tuple[dict[str, Any], bytes]:
        query = select(clinical_files).where(
            and_(
                clinical_files.c.tenant == principal.tenant,
                clinical_files.c.file_id == file_id,
                self._visible(principal),
            )
        )
        async with self.db.engine.connect() as conn:
            row = (await conn.execute(query)).first()
        if row is None:
            raise KeyError(file_id)
        await self.audit.record(
            principal.tenant,
            principal.id,
            "clinical_file.read",
            f"clinical_file/{file_id}",
            subject_id=row.patient_id,
        )
        return _public(row), bytes(row.content)

    async def export_subject(self, tenant: str, patient_id: str) -> dict[str, Any]:
        """For an access request: the list of files (not their bytes, which are handed
        over separately), with restricted ones counted, like psychotherapy notes."""
        query = select(*METADATA).where(
            and_(clinical_files.c.tenant == tenant, clinical_files.c.patient_id == patient_id)
        )
        async with self.db.engine.connect() as conn:
            rows = [_public(r) for r in await conn.execute(query)]
        shared = [r for r in rows if r["access"] != "author_only"]
        return {"files": shared, "restricted_files_withheld": len(rows) - len(shared)}
