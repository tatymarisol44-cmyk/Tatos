"""Clinician-only, author-private test source drafts and explicit incorporation."""

from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi import Path as APIPath
from pydantic import BaseModel, Field, ValidationError

from orchestrator.api.billing import no_store
from orchestrator.api.security import require_clinician
from orchestrator.auth import Principal
from orchestrator.instrument_imports import (
    MAX_BYTES,
    ImportRejected,
    ImportReview,
    InstrumentImports,
    SourceManifest,
)

router = APIRouter(
    prefix="/v1/clinical/instrument-imports",
    tags=["instrument sources"],
    dependencies=[Depends(no_store)],
)
Clinician = Annotated[Principal, Depends(require_clinician)]
ID = Annotated[str, APIPath(pattern=r"^[0-9a-f]{16}$")]


def service(request: Request) -> InstrumentImports:
    return cast(InstrumentImports, request.app.state.orchestrator.instrument_imports)


def error(exc: Exception) -> HTTPException:
    return (
        HTTPException(404, "source draft not found")
        if isinstance(exc, KeyError)
        else HTTPException(422, str(exc))
    )


@router.get("")
async def listing(request: Request, p: Clinician) -> list[dict[str, Any]]:
    return await service(request).listing(p)


@router.post("", status_code=201)
async def upload(
    request: Request,
    p: Clinician,
    file: Annotated[UploadFile, File()],
    manifest: Annotated[str, Form(max_length=8000)],
) -> dict[str, Any]:
    try:
        source = SourceManifest.model_validate_json(manifest)
        return await service(request).upload(
            p, source, file.filename or "source", await file.read(MAX_BYTES + 1)
        )
    except ValidationError as exc:
        raise HTTPException(
            422, "complete the source, edition, licence and permitted-use fields"
        ) from exc
    except ImportRejected as exc:
        raise error(exc) from exc


@router.get("/{draft_id}")
async def detail(draft_id: ID, request: Request, p: Clinician) -> dict[str, Any]:
    try:
        return await service(request).get(p, draft_id)
    except KeyError as exc:
        raise error(exc) from exc


@router.post("/{draft_id}/incorporate")
async def incorporate(
    draft_id: ID, body: ImportReview, request: Request, p: Clinician
) -> dict[str, Any]:
    try:
        return await service(request).promote(p, draft_id, body)
    except (KeyError, ImportRejected) as exc:
        raise error(exc) from exc


class RevisionIn(BaseModel):
    revision: int = Field(ge=1)


@router.post("/{draft_id}/purge")
async def purge(draft_id: ID, body: RevisionIn, request: Request, p: Clinician) -> dict[str, bool]:
    try:
        await service(request).purge(p, draft_id, body.revision)
        return {"purged": True}
    except (KeyError, ImportRejected) as exc:
        raise error(exc) from exc
