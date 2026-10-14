"""Who is signed in (`GET /v1/session`): what the staff app needs to greet the person and
show their own agenda. It names the key's roles and the professional it belongs to, never
the key itself. Patients use `/v1/me`."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request

from orchestrator.api.security import require_staff
from orchestrator.auth import Principal

router = APIRouter(prefix="/v1/session", tags=["session"])
Staff = Annotated[Principal, Depends(require_staff)]


@router.get("")
async def session(request: Request, p: Staff) -> dict[str, Any]:
    orch = request.app.state.orchestrator
    professional = await orch.professionals.for_staff(p.tenant, p.id)
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
    }
