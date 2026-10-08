"""Psychological instruments (tests, scales, questionnaires) and their results.

A clinician defines any instrument (or starts from a public-domain template), applies it
to a patient and gets the score, band and alerts. Results belong to the clinical record:
clinicians only (reception has no access), every action audited without the answers."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi import Path as FastAPIPath
from pydantic import BaseModel, Field

from orchestrator.api.security import requires
from orchestrator.auth import Principal, Role
from orchestrator.instruments import (
    TEMPLATES,
    InstrumentError,
    Instruments,
    InstrumentSpec,
)

router = APIRouter(prefix="/v1/clinical", tags=["instruments"])
Clinician = Annotated[Principal, Depends(requires(Role.REVIEWER))]
PatientId = Annotated[str, FastAPIPath(pattern=r"^[\w.-]{1,64}$")]
InstrumentId = Annotated[str, FastAPIPath(pattern=r"^[\w-]{1,40}$")]
ResultId = Annotated[str, FastAPIPath(pattern=r"^[0-9a-f]{1,32}$")]


class InstrumentIn(BaseModel):
    spec: InstrumentSpec
    visibility: Literal["private", "establishment"] = Field(
        default="establishment", description="private: only its author can see and use it"
    )


class FromTemplateIn(BaseModel):
    template: Literal["phq9", "gad7"]
    visibility: Literal["private", "establishment"] = "establishment"


class AdministerIn(BaseModel):
    instrument_id: str = Field(pattern=r"^[\w-]{1,40}$")
    version: int | None = Field(default=None, ge=1, description="Default: the latest")
    answers: dict[str, float | str | None]
    note: str | None = Field(default=None, max_length=2000)


def service(request: Request) -> Instruments:
    return request.app.state.orchestrator.instruments  # type: ignore[no-any-return]


@router.get("/instrument-templates")
async def templates(p: Clinician) -> dict[str, Any]:
    """Public-domain instruments ready to copy into the practice."""
    return {key: spec.model_dump(mode="json") for key, spec in TEMPLATES.items()}


@router.post("/instruments", status_code=status.HTTP_201_CREATED)
async def create_instrument(body: InstrumentIn, request: Request, p: Clinician) -> dict[str, Any]:
    try:
        return await service(request).create(p, body.spec, body.visibility)
    except InstrumentError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc


@router.post("/instruments/from-template", status_code=status.HTTP_201_CREATED)
async def create_from_template(
    body: FromTemplateIn, request: Request, p: Clinician
) -> dict[str, Any]:
    return await service(request).create(p, TEMPLATES[body.template], body.visibility)


@router.get("/instruments")
async def list_instruments(request: Request, p: Clinician) -> list[dict[str, Any]]:
    return await service(request).available(p)


@router.get("/instruments/{instrument_id}")
async def get_instrument(
    instrument_id: InstrumentId, request: Request, p: Clinician, version: int | None = None
) -> dict[str, Any]:
    try:
        return await service(request).get(p, instrument_id, version)
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "instrument not found") from exc


@router.put("/instruments/{instrument_id}")
async def revise_instrument(
    instrument_id: InstrumentId, body: InstrumentIn, request: Request, p: Clinician
) -> dict[str, Any]:
    """Save a new version. Results already recorded keep the version they were scored with."""
    try:
        return await service(request).revise(p, instrument_id, body.spec)
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "instrument not found") from exc
    except PermissionError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc


@router.delete("/instruments/{instrument_id}", status_code=status.HTTP_204_NO_CONTENT)
async def retire_instrument(instrument_id: InstrumentId, request: Request, p: Clinician) -> None:
    try:
        await service(request).retire(p, instrument_id)
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "instrument not found") from exc
    except PermissionError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc


@router.post("/patients/{patient_id}/instrument-results", status_code=status.HTTP_201_CREATED)
async def administer(
    patient_id: PatientId, body: AdministerIn, request: Request, p: Clinician
) -> dict[str, Any]:
    """Record a patient's answers; returns the score, band and any alert to act on."""
    try:
        return await service(request).administer(
            p, patient_id, body.instrument_id, body.answers, body.version, body.note
        )
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "patient or instrument not found") from exc
    except InstrumentError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc


@router.get("/patients/{patient_id}/instrument-results")
async def list_results(
    patient_id: PatientId, request: Request, p: Clinician
) -> list[dict[str, Any]]:
    try:
        return await service(request).results(p, patient_id)
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "patient not found") from exc


@router.get("/instrument-results/{result_id}")
async def get_result(result_id: ResultId, request: Request, p: Clinician) -> dict[str, Any]:
    try:
        return await service(request).result(p, result_id)
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "result not found") from exc
