"""The clinical record: entries written by clinicians (ADR 0014). Reception has no access;
a psychotherapy note is visible to its author only."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi import Path as FastAPIPath
from pydantic import BaseModel, Field

from orchestrator.api.security import requires
from orchestrator.auth import Principal, Role
from orchestrator.clinical_records import MAX_BODY, ClinicalError, ClinicalRecords

router = APIRouter(prefix="/v1/clinical", tags=["clinical"])
Clinician = Annotated[Principal, Depends(requires(Role.REVIEWER))]
PatientId = Annotated[str, FastAPIPath(pattern=r"^[\w.-]{1,64}$")]
DocumentId = Annotated[str, FastAPIPath(pattern=r"^[0-9a-f]{1,32}$")]


class DocumentIn(BaseModel):
    doc_type: str = Field(pattern=r"^[\w-]{1,60}$", description="A document id of your pack")
    body: str = Field(min_length=1, max_length=MAX_BODY)
    amends: str | None = Field(default=None, pattern=r"^[0-9a-f]{1,32}$")


def service(request: Request) -> ClinicalRecords:
    return request.app.state.orchestrator.clinical  # type: ignore[no-any-return]


@router.post("/patients/{patient_id}/documents", status_code=status.HTTP_201_CREATED)
async def write_document(
    body: DocumentIn, patient_id: PatientId, request: Request, p: Clinician
) -> dict[str, Any]:
    try:
        return await service(request).write(
            p, patient_id, doc_type=body.doc_type, body=body.body, amends=body.amends
        )
    except ClinicalError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc


@router.get("/patients/{patient_id}/documents")
async def list_documents(
    patient_id: PatientId, request: Request, p: Clinician
) -> list[dict[str, Any]]:
    return await service(request).list(p, patient_id)


@router.get("/documents/{document_id}")
async def get_document(document_id: DocumentId, request: Request, p: Clinician) -> dict[str, Any]:
    try:
        return await service(request).get(p, document_id)
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "document not found") from exc
