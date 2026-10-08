"""The agenda: professionals' working hours, free slots, who sees whom when, and
iCalendar feeds for phones. Bookings themselves go through /v1/crm/appointments (staff)
and /v1/me/appointments (patients), both refusing any overlap."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi import Path as FastAPIPath
from pydantic import BaseModel, Field

from orchestrator.agenda import Agenda, AgendaError, Block
from orchestrator.api.security import require_staff, requires
from orchestrator.auth import Principal, Role

router = APIRouter(prefix="/v1/agenda", tags=["agenda"])
Staff = Annotated[Principal, Depends(require_staff)]
Scheduler = Annotated[Principal, Depends(requires(Role.RECEPTION))]  # admin implies it
CareReader = Annotated[Principal, Depends(requires(Role.RECEPTION, Role.REVIEWER, Role.OWNER))]
ProfessionalId = Annotated[str, FastAPIPath(pattern=r"^[\w.@-]{1,64}$")]


class HoursBlock(BaseModel):
    day: str = Field(pattern=r"^(mon|tue|wed|thu|fri|sat|sun)$")
    hours: str = Field(pattern=r"^\d{2}:\d{2}-\d{2}:\d{2}$", examples=["09:00-13:00"])
    slot_minutes: int = Field(default=50, ge=10, le=240)


class HoursIn(BaseModel):
    hours: list[HoursBlock] = Field(max_length=50)


def agenda(request: Request) -> Agenda:
    return request.app.state.orchestrator.agenda  # type: ignore[no-any-return]


@router.put("/professionals/{professional_id}/hours")
async def set_hours(
    professional_id: ProfessionalId, body: HoursIn, request: Request, p: Scheduler
) -> list[dict[str, Any]]:
    """Replace the professional's weekly hours (local time of the practice)."""
    try:
        blocks = [Block.parse(b.day, b.hours, b.slot_minutes) for b in body.hours]
        return await agenda(request).set_hours(p.tenant, professional_id, blocks, p.id)
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "professional not found") from exc
    except AgendaError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc


@router.get("/professionals/{professional_id}/hours")
async def get_hours(
    professional_id: ProfessionalId, request: Request, p: Staff
) -> list[dict[str, Any]]:
    try:
        return await agenda(request).hours(p.tenant, professional_id)
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "professional not found") from exc


@router.get("/slots")
async def slots(
    request: Request,
    p: Staff,
    professional_id: Annotated[str, Query(pattern=r"^[\w.@-]{1,64}$")],
    days: Annotated[int, Query(ge=1, le=60)] = 7,
) -> list[dict[str, Any]]:
    try:
        return await agenda(request).free_slots(p.tenant, professional_id, days=days)
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "professional not found") from exc


@router.get("")
async def view(
    request: Request,
    p: CareReader,
    professional_id: Annotated[str | None, Query(pattern=r"^[\w.@-]{1,64}$")] = None,
    start: Annotated[date | None, Query(alias="from")] = None,
    days: Annotated[int, Query(ge=1, le=62)] = 7,
) -> list[dict[str, Any]]:
    """Appointments from `from` (default today) for `days`, one professional or all."""
    a = agenda(request)
    first = start or datetime.now(a.tz).date()
    begin = datetime.combine(first, time(0), a.tz).astimezone(UTC)
    return await a.appointments_for(
        p.tenant, professional_id=professional_id, start=begin, end=begin + timedelta(days=days)
    )


@router.post("/professionals/{professional_id}/calendar-link")
async def calendar_link(
    professional_id: ProfessionalId, request: Request, p: CareReader
) -> dict[str, str]:
    """A private iCalendar URL for the professional's phone (initials and times only)."""
    a = agenda(request)
    try:
        await a.hours(p.tenant, professional_id)  # the professional exists in this tenant
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "professional not found") from exc
    base = request.app.state.settings.public_base_url.rstrip("/")
    return {"url": base + a.feed_path(p.tenant, "professional", professional_id)}
