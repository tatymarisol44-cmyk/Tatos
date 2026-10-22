"""The patient's own endpoints (`/v1/me`). Only patient keys open them, and the subject is
always the key's: a patient cannot name another subject, see drafts held for review,
internal routing, risk reasons, segments or anyone else's records."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi import Path as FastAPIPath
from pydantic import AwareDatetime, BaseModel, Field

from orchestrator.agenda import AgendaError
from orchestrator.api.schemas import PatientChatIn, PatientConsentIn
from orchestrator.api.security import require_patient
from orchestrator.auth import Principal
from orchestrator.crm import CrmError
from orchestrator.governance import Purpose, ThreadBusyError
from orchestrator.instruments import InstrumentError
from orchestrator.service import Orchestrator, PendingReviewError
from orchestrator.workshops import MAX_COMPANIONS, WorkshopError

router = APIRouter(prefix="/v1/me", tags=["patient"])
Me = Annotated[Principal, Depends(require_patient)]


def orch(request: Request) -> Orchestrator:
    return request.app.state.orchestrator  # type: ignore[no-any-return]


def _subject(p: Principal) -> str:
    assert p.subject_id is not None  # guaranteed by require_patient
    return p.subject_id


async def _profile(request: Request, p: Principal) -> dict[str, Any]:
    try:
        return await orch(request).portal.profile(p.tenant, _subject(p))
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "patient record not available") from exc


@router.get("")
async def me(request: Request, p: Me) -> dict[str, Any]:
    """Everything the patient may see: profile, appointments, plans, loyalty, offers."""
    return await _profile(request, p)


@router.get("/appointments")
async def my_appointments(request: Request, p: Me) -> dict[str, Any]:
    profile = await _profile(request, p)
    return {"upcoming": profile["upcoming_appointments"], "past": profile["past_appointments"]}


@router.get("/treatments")
async def my_treatments(request: Request, p: Me) -> list[dict[str, Any]]:
    plans: list[dict[str, Any]] = (await _profile(request, p))["treatment_plans"]
    return plans


@router.get("/loyalty")
async def my_loyalty(request: Request, p: Me) -> dict[str, Any]:
    """Visits, offers and the loyalty tier with its benefits (RF-MKT-02)."""
    profile = await _profile(request, p)
    tier = await orch(request).loyalty.for_patient(p.tenant, _subject(p))
    return {**profile["loyalty"], "offers": profile["offers"], "tier": tier}


@router.post("/offers/seen")
async def offers_seen(request: Request, p: Me) -> dict[str, int]:
    """The app showed the patient their offers: none of them goes out by Telegram."""
    return {"marked": await orch(request).campaigns.mark_seen(p.tenant, _subject(p))}


@router.get("/consents")
async def my_consent_prompt(request: Request, p: Me) -> dict[str, Any]:
    """Call on first sign-in and before booking: `ask` lists the consents not answered
    yet (each is asked once). Show `yes_label` and `no_label` with equal weight, neither
    pre-selected, and always the `footer`; then PUT each answer."""
    return await orch(request).portal.consent_prompt(p.tenant, _subject(p))


@router.put("/consents/{purpose}")
async def set_my_consent(
    body: PatientConsentIn, request: Request, purpose: Purpose, p: Me
) -> dict[str, Any]:
    """Opt in or out (marketing, memory, photos, analytics) from the patient's own app."""
    try:
        return await orch(request).portal.set_own_consent(
            p.tenant, _subject(p), purpose, body.granted
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc


@router.post("/chat")
async def my_chat(body: PatientChatIn, request: Request, p: Me) -> dict[str, Any]:
    """Ask the clinic's assistant. It sees this patient's records only; answers that need
    a professional come back as `pending_review` and appear later on GET /chat/{id}."""
    await _profile(request, p)  # restricted or missing record: 404 before any model call
    try:
        return await orch(request).portal.chat(p.tenant, _subject(p), body.question, body.thread_id)
    except PendingReviewError as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "your previous question is still being reviewed"
        ) from exc
    except ThreadBusyError as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "your previous question is still being answered"
        ) from exc


@router.get("/chat/{thread_id}")
async def my_thread(
    request: Request,
    p: Me,
    thread_id: Annotated[str, FastAPIPath(max_length=64, pattern=r"^[\w-]+$")],
) -> dict[str, Any]:
    try:
        return await orch(request).portal.thread(p.tenant, _subject(p), thread_id)
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "conversation not found") from exc


# --- booking from the patient's app ------------------------------------------------------


class BookIn(BaseModel):
    professional_id: str = Field(pattern=r"^[\w.@-]{1,64}$")
    starts_at: AwareDatetime


class SignUpIn(BaseModel):
    companions: int = Field(default=0, ge=0, le=MAX_COMPANIONS)


@router.get("/workshops")
async def my_workshops(request: Request, p: Me) -> list[dict[str, Any]]:
    """Upcoming workshops and talks of the practice, with the patient's own sign-up."""
    tier = await orch(request).loyalty.for_patient(p.tenant, _subject(p))
    return await orch(request).workshops.for_patient(
        p.tenant, _subject(p), early_hours=tier["early_hours"]
    )


@router.post("/workshops/{workshop_id}")
async def sign_up(
    workshop_id: Annotated[str, FastAPIPath(pattern=r"^[0-9a-f]{1,32}$")],
    body: SignUpIn,
    request: Request,
    p: Me,
) -> dict[str, Any]:
    try:
        tier = await orch(request).loyalty.for_patient(p.tenant, _subject(p))
        return await orch(request).workshops.register(
            p.tenant, workshop_id, _subject(p), body.companions, early_hours=tier["early_hours"]
        )
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "workshop not found") from exc
    except WorkshopError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc


@router.delete("/workshops/{workshop_id}", status_code=status.HTTP_204_NO_CONTENT)
async def withdraw(
    workshop_id: Annotated[str, FastAPIPath(pattern=r"^[0-9a-f]{1,32}$")],
    request: Request,
    p: Me,
) -> None:
    try:
        if not await orch(request).workshops.withdraw(p.tenant, workshop_id, _subject(p)):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no sign-up to withdraw")
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "workshop not found") from exc
    except WorkshopError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc


@router.get("/practice")
async def my_practice(request: Request, p: Me) -> dict[str, Any]:
    """The practice's name and its active professionals (names only), for the patient's
    app to show and to book with."""
    o = orch(request)
    team = await o.professionals.list(p.tenant)
    return {
        "practice": o.settings.practice_display_name,
        "timezone": o.settings.clinic_timezone,
        "professionals": [
            {"professional_id": t["professional_id"], "display_name": t["display_name"]}
            for t in team
            if t["active"]
        ],
    }


@router.get("/slots")
async def my_slots(
    request: Request,
    p: Me,
    professional_id: Annotated[str, Query(pattern=r"^[\w.@-]{1,64}$")],
    days: Annotated[int, Query(ge=1, le=60)] = 14,
) -> list[dict[str, Any]]:
    """Free times of a professional of the patient's practice, as far ahead as the
    patient's loyalty tier allows (RF-MKT-02)."""
    tier = await orch(request).loyalty.for_patient(p.tenant, _subject(p))
    days = min(days, tier["booking_days"])
    try:
        return await orch(request).agenda.free_slots(p.tenant, professional_id, days=days)
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "professional not found") from exc


@router.get("/booking-status")
async def booking_status(
    request: Request,
    p: Me,
    professional_id: Annotated[str, Query(pattern=r"^[\w.@-]{1,64}$")],
) -> dict[str, bool]:
    """Distinguish unpublished working hours from a calendar without free slots."""
    await _profile(request, p)
    try:
        hours = await orch(request).agenda.hours(p.tenant, professional_id)
    except KeyError as exc:
        raise HTTPException(404, "professional not found") from exc
    return {"hours_configured": bool(hours)}


@router.post("/appointments", status_code=status.HTTP_201_CREATED)
async def book(body: BookIn, request: Request, p: Me) -> dict[str, Any]:
    """Book one of the free slots (only those: a patient cannot pick an arbitrary time)."""
    o = orch(request)
    try:
        minutes = await o.agenda.is_free_slot(p.tenant, body.professional_id, body.starts_at)
        made = await o.crm.create_appointment(
            p.tenant,
            _subject(p),
            starts_at=body.starts_at,
            duration_min=minutes,
            kind="sesion",
            price=0,
            actor=p.id,
            professional_id=body.professional_id,
            require_available_slot=True,
        )
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "professional not found") from exc
    except (AgendaError, CrmError) as exc:  # SlotTaken is a CrmError: taken meanwhile
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return {k: made[k] for k in ("id", "starts_at", "duration_min", "status", "professional_id")}


@router.get("/calendar-link")
async def my_calendar_link(request: Request, p: Me) -> dict[str, str]:
    """A private iCalendar URL for the patient's own appointments."""
    base = request.app.state.settings.public_base_url.rstrip("/")
    return {"url": base + orch(request).agenda.feed_path(p.tenant, "patient", _subject(p))}


# --- tests sent by the professional (RF-TST-03) ----------------------------------------------


class AnswersIn(BaseModel):
    answers: dict[str, float | str | None] = Field(max_length=200)


@router.get("/tests")
async def my_tests(request: Request, p: Me) -> list[dict[str, Any]]:
    """The tests the professional sent and the patient has not answered yet."""
    return await orch(request).self_reports.pending(p.tenant, _subject(p))


@router.post("/tests/{assignment_id}")
async def answer_test(
    assignment_id: Annotated[str, FastAPIPath(pattern=r"^[0-9a-f]{1,32}$")],
    body: AnswersIn,
    request: Request,
    p: Me,
) -> dict[str, Any]:
    """Send the answers. The reply is a thank-you, never the score; when an answer needs a
    person now, it carries the emergency lines too."""
    try:
        return await orch(request).self_reports.submit(
            p.tenant, _subject(p), p.id, assignment_id, body.answers
        )
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no pending test with this id") from exc
    except InstrumentError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
