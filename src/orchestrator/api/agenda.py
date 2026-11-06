"""The agenda: professionals' working hours, free slots, who sees whom when, and
iCalendar feeds for phones. Bookings themselves go through /v1/crm/appointments (staff)
and /v1/me/appointments (patients), both refusing any overlap."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi import Path as FastAPIPath
from pydantic import AwareDatetime, BaseModel, Field

from orchestrator.agenda import Agenda, AgendaError, Block
from orchestrator.api.security import require_staff, requires
from orchestrator.auth import Principal, Role
from orchestrator.availability import AvailabilityError, DayBlock, SchedulePolicy
from orchestrator.crm import CrmError, NotFoundError
from orchestrator.service import Orchestrator

router = APIRouter(prefix="/v1/agenda", tags=["agenda"])
Staff = Annotated[Principal, Depends(require_staff)]
Scheduler = Annotated[Principal, Depends(requires(Role.RECEPTION, Role.OWNER, Role.REVIEWER))]
CareReader = Annotated[Principal, Depends(requires(Role.RECEPTION, Role.REVIEWER, Role.OWNER))]
ProfessionalId = Annotated[str, FastAPIPath(pattern=r"^[\w.@-]{1,64}$")]


class HoursBlock(BaseModel):
    day: str = Field(pattern=r"^(mon|tue|wed|thu|fri|sat|sun)$")
    hours: str = Field(pattern=r"^\d{2}:\d{2}-\d{2}:\d{2}$", examples=["09:00-13:00"])
    slot_minutes: int = Field(default=50, ge=10, le=240)
    break_minutes: int = Field(default=0, ge=0, le=120)


class HoursIn(BaseModel):
    hours: list[HoursBlock] = Field(max_length=50)


def agenda(request: Request) -> Agenda:
    return request.app.state.orchestrator.agenda  # type: ignore[no-any-return]


def orchestrator(request: Request) -> Orchestrator:
    return cast(Orchestrator, request.app.state.orchestrator)


async def editable(request: Request, p: Principal, professional_id: str) -> None:
    if p.has(Role.RECEPTION, Role.OWNER):
        return
    own = await request.app.state.orchestrator.professionals.for_staff(p.tenant, p.id)
    if not own or own["professional_id"] != professional_id:
        raise HTTPException(403, "only your own professional schedule")


class PolicyIn(BaseModel):
    version: int = Field(ge=1)
    policy: SchedulePolicy


class ExceptionsIn(BaseModel):
    version: int = Field(ge=1)
    first: date
    last: date
    blocks: list[DayBlock] | None = Field(default_factory=list, max_length=20)


class RescheduleIn(BaseModel):
    starts_at: AwareDatetime
    service_id: str | None = Field(default=None, pattern=r"^[\w-]{1,32}$")
    schedule_version: int | None = Field(default=None, ge=1)


@router.post("/appointments/{appointment_id}/reschedule")
async def reschedule(
    appointment_id: Annotated[str, FastAPIPath(pattern=r"^[0-9a-f]{1,32}$")],
    body: RescheduleIn,
    request: Request,
    p: Scheduler,
) -> dict[str, Any]:
    o = orchestrator(request)
    try:
        current = await o.crm.get_appointment(p.tenant, appointment_id)
        if not current["professional_id"]:
            raise AgendaError("assign a professional before rescheduling")
        await editable(request, p, current["professional_id"])
        slot = await o.agenda.slot_details(
            p.tenant,
            current["professional_id"],
            body.starts_at,
            service_id=body.service_id,
            exclude_id=appointment_id,
        )
        if body.schedule_version is not None and body.schedule_version != slot["schedule_version"]:
            raise AgendaError("the schedule changed; reload availability")
        return await o.crm.reschedule(p.tenant, appointment_id, body.starts_at, slot, p.id)
    except (NotFoundError, KeyError) as exc:
        raise HTTPException(404, "appointment not found") from exc
    except (CrmError, AgendaError) as exc:
        raise HTTPException(409, str(exc)) from exc


@router.get("/professionals/{professional_id}/availability")
async def availability(
    professional_id: ProfessionalId, request: Request, p: Staff
) -> dict[str, Any]:
    try:
        return await orchestrator(request).availability.get(p.tenant, professional_id)
    except KeyError as exc:
        raise HTTPException(404, "professional not found") from exc


@router.put("/professionals/{professional_id}/availability")
async def update_availability(
    professional_id: ProfessionalId,
    body: PolicyIn,
    request: Request,
    p: Scheduler,
) -> dict[str, Any]:
    await editable(request, p, professional_id)
    try:
        return await orchestrator(request).availability.policy(
            p.tenant, professional_id, body.policy, body.version, p.id
        )
    except KeyError as exc:
        raise HTTPException(404, "professional not found") from exc
    except AvailabilityError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.put("/professionals/{professional_id}/exceptions")
async def update_exceptions(
    professional_id: ProfessionalId,
    body: ExceptionsIn,
    request: Request,
    p: Scheduler,
) -> dict[str, Any]:
    await editable(request, p, professional_id)
    try:
        return await orchestrator(request).availability.exception(
            p.tenant, professional_id, body.first, body.last, body.blocks, body.version, p.id
        )
    except KeyError as exc:
        raise HTTPException(404, "professional not found") from exc
    except AvailabilityError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.put("/professionals/{professional_id}/hours")
async def set_hours(
    professional_id: ProfessionalId, body: HoursIn, request: Request, p: Scheduler
) -> list[dict[str, Any]]:
    """Replace the professional's weekly hours (local time of the practice)."""
    await editable(request, p, professional_id)
    try:
        blocks = [Block.parse(b.day, b.hours, b.slot_minutes, b.break_minutes) for b in body.hours]
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
    on_date: date | None = None,
    service_id: Annotated[str | None, Query(pattern=r"^[\w-]{1,32}$")] = None,
) -> list[dict[str, Any]]:
    try:
        return await agenda(request).free_slots(
            p.tenant, professional_id, days=days, on_date=on_date, service_id=service_id
        )
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "professional not found") from exc
    except AgendaError as exc:
        raise HTTPException(409, str(exc)) from exc


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
