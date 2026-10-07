"""Publications on social networks (ADR 0015, M3): marketing creates and approves, the
owner too when a discount exceeds the pack's cap, and publishing happens once."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi import Path as FastAPIPath
from pydantic import BaseModel, Field

from orchestrator.api.security import require_staff, requires
from orchestrator.auth import Principal, Role
from orchestrator.creatives import Brief, CreativeRejected, CreativeUnavailable
from orchestrator.publishing import (
    MAX_CAPTION,
    Kind,
    OwnerApprovalRequired,
    PublicationError,
    PublicationService,
)

router = APIRouter(prefix="/v1/social/publications", tags=["social"])
Marketing = Annotated[Principal, Depends(requires(Role.MARKETING, Role.OWNER))]
Staff = Annotated[Principal, Depends(require_staff)]
PublicationId = Annotated[str, FastAPIPath(max_length=32, pattern=r"^[0-9a-f]+$")]


class PublicationIn(BaseModel):
    account_id: str = Field(max_length=32, pattern=r"^[0-9a-f]+$")
    kind: Kind
    format: Literal["feed", "story", "square"] | None = None
    caption: str = Field(min_length=1, max_length=MAX_CAPTION)
    brief: Brief


def service(request: Request) -> PublicationService:
    return request.app.state.orchestrator.publications  # type: ignore[no-any-return]


def _errors(exc: Exception) -> HTTPException:
    if isinstance(exc, KeyError):
        return HTTPException(status.HTTP_404_NOT_FOUND, "not found")
    if isinstance(exc, CreativeRejected):
        return HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, {"violations": exc.violations})
    if isinstance(exc, OwnerApprovalRequired):
        return HTTPException(status.HTTP_403_FORBIDDEN, str(exc))
    if isinstance(exc, CreativeUnavailable):
        return HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc))
    return HTTPException(status.HTTP_409_CONFLICT, str(exc))


_HANDLED = (
    KeyError,
    CreativeRejected,
    OwnerApprovalRequired,
    CreativeUnavailable,
    PublicationError,
)


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_publication(body: PublicationIn, request: Request, p: Marketing) -> dict[str, Any]:
    try:
        return await service(request).create(
            p.tenant,
            p.id,
            account_id=body.account_id,
            kind=body.kind,
            brief=body.brief,
            caption=body.caption,
            fmt=body.format,
        )
    except _HANDLED as exc:
        raise _errors(exc) from exc


@router.get("")
async def list_publications(request: Request, p: Staff) -> list[dict[str, Any]]:
    return await service(request).list(p.tenant)


@router.get("/{publication_id}")
async def get_publication(
    publication_id: PublicationId, request: Request, p: Staff
) -> dict[str, Any]:
    try:
        return await service(request).get(p.tenant, publication_id)
    except KeyError as exc:
        raise _errors(exc) from exc


@router.post("/{publication_id}/approve")
async def approve_publication(
    publication_id: PublicationId, request: Request, p: Marketing
) -> dict[str, Any]:
    """Who approves comes from the key, never from the request body."""
    try:
        return await service(request).approve(
            p.tenant, publication_id, p.id, owner_approval=p.has(Role.OWNER)
        )
    except _HANDLED as exc:
        raise _errors(exc) from exc


@router.post("/{publication_id}/publish")
async def publish_publication(
    publication_id: PublicationId, request: Request, p: Marketing
) -> dict[str, Any]:
    try:
        return await service(request).publish(p.tenant, publication_id, p.id)
    except _HANDLED as exc:
        raise _errors(exc) from exc


@router.post("/{publication_id}/cancel")
async def cancel_publication(
    publication_id: PublicationId, request: Request, p: Marketing
) -> dict[str, Any]:
    try:
        return await service(request).cancel(p.tenant, publication_id, p.id)
    except _HANDLED as exc:
        raise _errors(exc) from exc
