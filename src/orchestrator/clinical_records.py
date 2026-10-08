"""Documents written by professionals into a patient's record (ADR 0014).

* **Append-only.** Nothing is edited or deleted; a correction is a new entry that `amends`
  an earlier one, so the record shows what was written, when and by whom.
* **The type must exist in the author's profession pack** (`documents` of the pack of the
  professional whose staff key wrote it, or of the establishment). Patient-authored diary
  entries are not written here: they have their own store and rules (lawyer's I1, I3).
* **Who reads what.** A psychotherapy note (`access: author_only`) is visible to its author
  only, not to other clinicians and not to an admin key; it is not even listed for anyone
  else. Every other entry is visible to clinicians (role `reviewer`), not to reception.
* **Every read is audited**, with document ids and never their content.
* Nothing here is connected to retrieval, memory, insights, campaigns or models
  (`surfaces.HARD_EXCLUSIONS`): those modules have no path to this table."""

from __future__ import annotations

import hashlib
import uuid
from typing import Any

from sqlalchemy import Column, DateTime, String, Table, Text, and_, insert, or_, select

from orchestrator.auth import Principal, Role
from orchestrator.crm import patients
from orchestrator.db import Database, metadata, utcnow
from orchestrator.establishment import Professionals, professionals
from orchestrator.governance import AuditLog
from orchestrator.packs import Pack

MAX_BODY = 20_000

clinical_documents = Table(
    "clinical_documents",
    metadata,
    Column("tenant", String(64), primary_key=True),
    Column("document_id", String(32), primary_key=True),
    Column("patient_id", String(64), nullable=False, index=True),
    Column("doc_type", String(60), nullable=False),  # the pack's document id
    Column("kind", String(40), nullable=False),
    Column("access", String(24), nullable=False),
    Column("author_id", String(128), nullable=False),
    Column("professional_id", String(64), nullable=True),
    Column("pack_id", String(64), nullable=False),
    Column("body", Text, nullable=False),
    Column("sha256", String(64), nullable=False),
    Column("amends", String(32), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
)


class ClinicalError(ValueError):
    """Unknown patient, a type the author's pack does not have, or a bad amendment."""


def _public(row: Any) -> dict[str, Any]:
    return {c.name: getattr(row, c.name) for c in clinical_documents.c if c.name != "tenant"}


class ClinicalRecords:
    def __init__(self, db: Database, audit: AuditLog, professionals: Professionals) -> None:
        self.db = db
        self.audit = audit
        self.professionals = professionals

    async def _author_pack(self, principal: Principal) -> tuple[str | None, Pack]:
        """The pack of the professional who signs in with this key, else the tenant's."""
        query = select(professionals.c.professional_id).where(
            and_(
                professionals.c.tenant == principal.tenant,
                professionals.c.staff_id == principal.id,
                professionals.c.active.is_(True),
            )
        )
        async with self.db.engine.connect() as conn:
            found = (await conn.execute(query)).first()
        professional_id = found.professional_id if found else None
        return professional_id, await self.professionals.pack_for(principal.tenant, professional_id)

    def _visible(self, principal: Principal) -> Any:
        """Rows this principal may see: their own psychotherapy notes, and every other
        entry if they are a clinician."""
        own_restricted = and_(
            clinical_documents.c.access == "author_only",
            clinical_documents.c.author_id == principal.id,
        )
        if principal.has(Role.REVIEWER):
            return or_(clinical_documents.c.access != "author_only", own_restricted)
        return own_restricted

    async def write(
        self,
        principal: Principal,
        patient_id: str,
        *,
        doc_type: str,
        body: str,
        amends: str | None = None,
    ) -> dict[str, Any]:
        tenant = principal.tenant
        professional_id, pack = await self._author_pack(principal)
        spec = next((d for d in pack.documents if d.id == doc_type), None)
        if spec is None:
            raise ClinicalError(f"the {pack.id} pack has no document {doc_type!r}")
        if spec.kind == "patient_entry":
            raise ClinicalError("patient entries are written by the patient, not here")
        if not body.strip() or len(body) > MAX_BODY:
            raise ClinicalError(f"the text needs 1 to {MAX_BODY} characters")
        document_id = uuid.uuid4().hex[:16]
        async with self.db.engine.begin() as conn:
            exists = await conn.execute(
                select(patients.c.id).where(
                    and_(patients.c.tenant == tenant, patients.c.id == patient_id)
                )
            )
            if exists.first() is None:
                raise ClinicalError("unknown patient")
            if amends is not None:
                original = (
                    await conn.execute(
                        select(clinical_documents).where(
                            and_(
                                clinical_documents.c.tenant == tenant,
                                clinical_documents.c.document_id == amends,
                                clinical_documents.c.patient_id == patient_id,
                                self._visible(principal),
                            )
                        )
                    )
                ).first()
                if original is None or original.doc_type != doc_type:
                    raise ClinicalError("can only amend a visible entry of the same type")
            await conn.execute(
                insert(clinical_documents).values(
                    tenant=tenant,
                    document_id=document_id,
                    patient_id=patient_id,
                    doc_type=doc_type,
                    kind=spec.kind,
                    access=spec.access,
                    author_id=principal.id,
                    professional_id=professional_id,
                    pack_id=pack.id,
                    body=body,
                    sha256=hashlib.sha256(body.encode()).hexdigest(),
                    amends=amends,
                    created_at=utcnow(),
                )
            )
            await self.audit.record_in(
                conn,
                tenant,
                principal.id,
                "clinical_document.written",
                f"clinical_document/{document_id}",
                subject_id=patient_id,
                details={"doc_type": doc_type, "amends": amends},
            )
        return await self.get(principal, document_id)

    async def get(self, principal: Principal, document_id: str) -> dict[str, Any]:
        query = select(clinical_documents).where(
            and_(
                clinical_documents.c.tenant == principal.tenant,
                clinical_documents.c.document_id == document_id,
                self._visible(principal),
            )
        )
        async with self.db.engine.connect() as conn:
            row = (await conn.execute(query)).first()
        if row is None:  # someone else's psychotherapy note looks exactly like a missing one
            raise KeyError(document_id)
        await self.audit.record(
            principal.tenant,
            principal.id,
            "clinical_document.read",
            f"clinical_document/{document_id}",
            subject_id=row.patient_id,
        )
        return _public(row)

    async def export_subject(self, tenant: str, patient_id: str) -> dict[str, Any]:
        """For a data-subject access request: the clinical record, without psychotherapy
        notes, whose disclosure to the patient is the lawyer's question D2
        (owner's decision P8). Their number is reported so the privacy officer can act."""
        query = (
            select(clinical_documents)
            .where(
                and_(
                    clinical_documents.c.tenant == tenant,
                    clinical_documents.c.patient_id == patient_id,
                )
            )
            .order_by(clinical_documents.c.created_at, clinical_documents.c.document_id)
        )
        async with self.db.engine.connect() as conn:
            rows = list(await conn.execute(query))
        shared = [_public(r) for r in rows if r.access != "author_only"]
        return {"documents": shared, "psychotherapy_notes_withheld": len(rows) - len(shared)}

    async def list(self, principal: Principal, patient_id: str) -> list[dict[str, Any]]:
        """Raises `KeyError` for a patient this tenant does not have (another tenant's
        patient id answers like a made-up one, not with an empty record)."""
        known = select(patients.c.id).where(
            and_(patients.c.tenant == principal.tenant, patients.c.id == patient_id)
        )
        query = (
            select(clinical_documents)
            .where(
                and_(
                    clinical_documents.c.tenant == principal.tenant,
                    clinical_documents.c.patient_id == patient_id,
                    self._visible(principal),
                )
            )
            .order_by(clinical_documents.c.created_at, clinical_documents.c.document_id)
        )
        async with self.db.engine.connect() as conn:
            if (await conn.execute(known)).first() is None:
                raise KeyError(patient_id)
            rows = [_public(r) for r in await conn.execute(query)]
        await self.audit.record(
            principal.tenant,
            principal.id,
            "clinical_document.listed",
            f"patient/{patient_id}",
            subject_id=patient_id,
            details={"documents": [r["document_id"] for r in rows]},
        )
        return rows
