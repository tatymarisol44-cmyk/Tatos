"""The establishment's on-call list (ADR 0017): who is told about an alert, by level."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi import Path as FastAPIPath
from pydantic import BaseModel, Field

from orchestrator.api.security import requires
from orchestrator.auth import Principal, Role
from orchestrator.oncall import MAX_LEVEL, OnCall, OnCallError

router = APIRouter(prefix="/v1/admin/on-call", tags=["admin"])
Admin = Annotated[Principal, Depends(requires(Role.ADMIN))]
Care = Annotated[Principal, Depends(requires(Role.RECEPTION, Role.REVIEWER, Role.OWNER))]


class ContactIn(BaseModel):
    display_name: str = Field(min_length=1, max_length=120)
    level: int = Field(ge=1, le=MAX_LEVEL, description="1 = on duty, 2 = backup, ...")
    telegram_chat_id: str | None = Field(default=None, pattern=r"^-?\d{1,20}$")
    whatsapp_number: str | None = Field(default=None, max_length=24, pattern=r"^[+\d ()-]+$")
    email: str | None = Field(default=None, max_length=254)


def service(request: Request) -> OnCall:
    return request.app.state.orchestrator.oncall  # type: ignore[no-any-return]


@router.post("", status_code=status.HTTP_201_CREATED)
async def add_contact(body: ContactIn, request: Request, p: Admin) -> dict[str, Any]:
    try:
        return await service(request).add_contact(p.tenant, p.id, **body.model_dump())
    except OnCallError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc


@router.get("")
async def list_contacts(request: Request, p: Care) -> list[dict[str, Any]]:
    return await service(request).contacts(p.tenant)


@router.delete("/{contact_id}", status_code=status.HTTP_204_NO_CONTENT)
async def disable_contact(
    request: Request,
    contact_id: Annotated[str, FastAPIPath(pattern=r"^[0-9a-f]{1,32}$")],
    p: Admin,
) -> None:
    if not await service(request).disable_contact(p.tenant, contact_id, p.id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "active contact not found")
