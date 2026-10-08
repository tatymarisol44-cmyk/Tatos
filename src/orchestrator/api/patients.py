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
from orchestrator.service import Orchestrator, PendingReviewError

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
    profile = await _profile(request, p)
    return {**profile["loyalty"], "offers": profile["offers"]}


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


@router.get("/slots")
async def my_slots(
    request: Request,
    p: Me,
    professional_id: Annotated[str, Query(pattern=r"^[\w.@-]{1,64}$")],
    days: Annotated[int, Query(ge=1, le=60)] = 14,
) -> list[dict[str, Any]]:
    """Free times of a professional of the patient's practice."""
    try:
        return await orch(request).agenda.free_slots(p.tenant, professional_id, days=days)
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "professional not found") from exc


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
