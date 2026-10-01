"""The patient's own endpoints (`/v1/me`). Only patient keys open them, and the subject is
always the key's: a patient cannot name another subject, see drafts held for review,
internal routing, risk reasons, segments or anyone else's records."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi import Path as FastAPIPath

from orchestrator.api.schemas import PatientChatIn, PatientConsentIn
from orchestrator.api.security import require_patient
from orchestrator.auth import Principal
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
        return await orch(request).patient_profile(p.tenant, _subject(p))
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


@router.put("/consents/{purpose}")
async def set_my_consent(
    body: PatientConsentIn, request: Request, purpose: Purpose, p: Me
) -> dict[str, Any]:
    """Opt in or out (marketing, memory, photos, analytics) from the patient's own app."""
    try:
        return await orch(request).set_own_consent(p.tenant, _subject(p), purpose, body.granted)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc


@router.post("/chat")
async def my_chat(body: PatientChatIn, request: Request, p: Me) -> dict[str, Any]:
    """Ask the clinic's assistant. It sees this patient's records only; answers that need
    a professional come back as `pending_review` and appear later on GET /chat/{id}."""
    await _profile(request, p)  # restricted or missing record: 404 before any model call
    try:
        return await orch(request).patient_chat(
            p.tenant, _subject(p), body.question, body.thread_id
        )
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
        return await orch(request).patient_thread(p.tenant, _subject(p), thread_id)
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "conversation not found") from exc
