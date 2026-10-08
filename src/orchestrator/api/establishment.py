"""The establishment's professionals, each with their own profession pack (ADR 0014)."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi import Path as FastAPIPath
from pydantic import BaseModel, Field

from orchestrator.api.security import require_staff, requires
from orchestrator.auth import Principal, Role
from orchestrator.establishment import ProfessionalError, Professionals, UnknownStaffError

router = APIRouter(prefix="/v1/admin/professionals", tags=["admin"])
Admin = Annotated[Principal, Depends(requires(Role.ADMIN))]
Staff = Annotated[Principal, Depends(require_staff)]
ID = r"^[\w.@-]{1,64}$"


class ProfessionalIn(BaseModel):
    professional_id: str = Field(pattern=ID, description="e.g. 'dra.vera'")
    display_name: str = Field(min_length=1, max_length=120)
    pack_id: str = Field(pattern=r"^[\w-]{1,64}$", description="e.g. 'ec-psychiatrist'")
    staff_id: str | None = Field(
        default=None, pattern=r"^[\w.@-]{1,128}$", description="Their staff key's name, if any"
    )


def service(request: Request) -> Professionals:
    return request.app.state.orchestrator.professionals  # type: ignore[no-any-return]


@router.post("", status_code=status.HTTP_201_CREATED)
async def add_professional(body: ProfessionalIn, request: Request, p: Admin) -> dict[str, Any]:
    try:
        return await service(request).add(
            p.tenant,
            p.id,
            professional_id=body.professional_id,
            display_name=body.display_name,
            pack_id=body.pack_id,
            staff_id=body.staff_id,
        )
    except UnknownStaffError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    except ProfessionalError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc


@router.get("")
async def list_professionals(request: Request, p: Staff) -> list[dict[str, Any]]:
    return await service(request).list(p.tenant)


@router.delete("/{professional_id}", status_code=status.HTTP_204_NO_CONTENT)
async def disable_professional(
    request: Request,
    professional_id: Annotated[str, FastAPIPath(pattern=ID)],
    p: Admin,
) -> None:
    if not await service(request).disable(p.tenant, professional_id, p.id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "active professional not found")
