"""Workshops and talks (SRS RF-MKT-05): the practice creates them, patients sign up from
their app (`/v1/me/workshops`, in api/patients.py)."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi import Path as FastAPIPath
from pydantic import AwareDatetime, BaseModel, Field

from orchestrator.api.security import requires
from orchestrator.auth import Principal, Role
from orchestrator.workshops import WorkshopError, Workshops

router = APIRouter(prefix="/v1/workshops", tags=["workshops"])
Organizer = Annotated[Principal, Depends(requires(Role.MARKETING, Role.OWNER))]
# Who signed up says who is a patient: the staff who look after patients.
CareReader = Annotated[Principal, Depends(requires(Role.RECEPTION, Role.REVIEWER, Role.OWNER))]
WorkshopId = Annotated[str, FastAPIPath(pattern=r"^[0-9a-f]{1,32}$")]


class WorkshopIn(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    description: str = Field(min_length=1, max_length=2000)
    starts_at: AwareDatetime
    duration_min: int = Field(default=90, ge=15, le=480)
    place: str = Field(min_length=1, max_length=200, examples=["En el consultorio", "En línea"])
    capacity: int = Field(ge=1, le=500)
    opens_at: AwareDatetime | None = Field(
        default=None, description="When sign-up opens for everyone (earlier for loyalty tiers)"
    )


class AttendanceIn(BaseModel):
    patient_id: str = Field(pattern=r"^[\w.-]{1,64}$")
    attended: bool


def service(request: Request) -> Workshops:
    return request.app.state.orchestrator.workshops  # type: ignore[no-any-return]


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_workshop(body: WorkshopIn, request: Request, p: Organizer) -> dict[str, Any]:
    try:
        return await service(request).create(p.tenant, p.id, **body.model_dump())
    except WorkshopError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc


@router.get("")
async def list_workshops(
    request: Request, p: CareReader, include_past: bool = False
) -> list[dict[str, Any]]:
    return await service(request).listing(p.tenant, include_past=include_past)


@router.get("/{workshop_id}/registrations")
async def registrations(
    workshop_id: WorkshopId, request: Request, p: CareReader
) -> list[dict[str, Any]]:
    try:
        return await service(request).registrations(p.tenant, workshop_id, p.id)
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "workshop not found") from exc


@router.post("/{workshop_id}/cancel", status_code=status.HTTP_204_NO_CONTENT)
async def cancel_workshop(workshop_id: WorkshopId, request: Request, p: Organizer) -> None:
    try:
        await service(request).cancel(p.tenant, workshop_id, p.id)
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "workshop not found") from exc


@router.post("/{workshop_id}/attendance", status_code=status.HTTP_204_NO_CONTENT)
async def mark_attendance(
    workshop_id: WorkshopId, body: AttendanceIn, request: Request, p: CareReader
) -> None:
    try:
        await service(request).mark(p.tenant, workshop_id, body.patient_id, body.attended, p.id)
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "registration not found") from exc
    except WorkshopError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
