"""The clinical record: entries written by clinicians (ADR 0014). Reception has no access;
a psychotherapy note is visible to its author only."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Request,
    Response,
    UploadFile,
    status,
)
from fastapi import Path as FastAPIPath
from pydantic import BaseModel, Field

from orchestrator.api.security import requires
from orchestrator.auth import Principal, Role
from orchestrator.clinical_files import ClinicalFiles, FileRejected
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
    try:
        return await service(request).list(p, patient_id)
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "patient not found") from exc


@router.get("/documents/{document_id}")
async def get_document(document_id: DocumentId, request: Request, p: Clinician) -> dict[str, Any]:
    try:
        return await service(request).get(p, document_id)
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "document not found") from exc


# --- clinical files: signed consents, external reports, scanned tests ---------------------

FileId = Annotated[str, FastAPIPath(pattern=r"^[0-9a-f]{1,32}$")]


def files(request: Request) -> ClinicalFiles:
    return request.app.state.orchestrator.clinical_files  # type: ignore[no-any-return]


@router.post("/patients/{patient_id}/files", status_code=status.HTTP_201_CREATED)
async def upload_file(
    patient_id: PatientId,
    request: Request,
    p: Clinician,
    file: Annotated[UploadFile, File(description="PDF, PNG, JPEG or plain text, up to 10 MB")],
    label: Annotated[str, Form(min_length=1, max_length=120)],
    access: Annotated[Literal["care_team", "author_only"], Form()] = "care_team",
) -> dict[str, Any]:
    """Attach a file to the patient's record. Its SHA-256 proves later that it is unchanged."""
    content = await file.read()
    try:
        return await files(request).upload(
            p,
            patient_id,
            filename=file.filename or "file",
            content=content,
            label=label,
            access=access,
        )
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "patient not found") from exc
    except FileRejected as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc


@router.get("/patients/{patient_id}/files")
async def list_files(patient_id: PatientId, request: Request, p: Clinician) -> list[dict[str, Any]]:
    try:
        return await files(request).for_patient(p, patient_id)
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "patient not found") from exc


@router.get("/files/{file_id}")
async def download_file(file_id: FileId, request: Request, p: Clinician) -> Response:
    """The file itself, as an attachment (never rendered inline by the browser)."""
    try:
        info, content = await files(request).download(p, file_id)
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "file not found") from exc
    return Response(
        content,
        media_type=info["media_type"],
        headers={
            "Content-Disposition": f'attachment; filename="{info["filename"]}"',
            "X-Content-SHA256": info["sha256"],
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )
