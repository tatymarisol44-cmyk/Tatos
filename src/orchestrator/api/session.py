"""Who is signed in (`GET /v1/session`): what the staff app needs to greet the person and
show their own agenda. It names the key's roles and the professional it belongs to, never
the key itself. Patients use `/v1/me`.

`POST /v1/session/email` lets a staff member add an e-mail login to their own account
(staff_login.py): the answer is their personal link, to choose a password and add an
authenticator app right away."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from orchestrator.api.security import require_staff
from orchestrator.auth import STAFF, Principal

router = APIRouter(prefix="/v1/session", tags=["session"])
Staff = Annotated[Principal, Depends(require_staff)]


@router.get("")
async def session(request: Request, p: Staff) -> dict[str, Any]:
    orch = request.app.state.orchestrator
    professional = await orch.professionals.for_staff(p.tenant, p.id)
    login = await orch.staff_logins.of(p.tenant, p.id) if p.kind == STAFF else None
    return {
        "id": p.id,
        "kind": p.kind,
        "roles": sorted(p.roles),
        "clinician": p.is_clinician,
        "practice": orch.settings.practice_display_name,
        "timezone": orch.settings.clinic_timezone,
        "professional": (
            {k: professional[k] for k in ("professional_id", "display_name", "pack_id")}
            if professional
            else None
        ),
        # The e-mail this person signs in with, and whether the password is chosen yet.
        "email_login": login,
    }


class OwnEmailIn(BaseModel):
    email: str = Field(min_length=3, max_length=254)


@router.post("/email")
async def add_own_email(body: OwnEmailIn, request: Request, p: Staff) -> dict[str, str]:
    """Add (or change, before it is used) the e-mail this person signs in with."""
    if p.kind != STAFF:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "only a person's own staff account")
    orch = request.app.state.orchestrator
    professional = await orch.professionals.for_staff(p.tenant, p.id)
    name = professional["display_name"] if professional else None
    try:
        return await orch.staff_logins.attach_own_email(p.tenant, p.id, body.email, name)  # type: ignore[no-any-return]
    except ValueError as exc:
        code = status.HTTP_422_UNPROCESSABLE_CONTENT if "invalid" in str(exc) else 409
        raise HTTPException(code, str(exc)) from exc
