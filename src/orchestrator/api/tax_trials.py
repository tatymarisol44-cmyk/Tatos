"""Private non-fiscal prototype. No endpoint issues an SRI document."""

from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi import Path as APIPath
from pydantic import BaseModel, Field

from orchestrator.api.billing import no_store
from orchestrator.api.security import requires
from orchestrator.auth import Principal, Role
from orchestrator.tax_trials import IssuerProfile, TaxTrialError, TaxTrials, TrialRequest

router = APIRouter(
    prefix="/v1/tax-trials", tags=["non-fiscal trials"], dependencies=[Depends(no_store)]
)
Owner = Annotated[Principal, Depends(requires(Role.OWNER))]
ID = Annotated[str, APIPath(pattern=r"^[0-9a-f]{16}$")]


class IssuerIn(BaseModel):
    id: str | None = Field(default=None, pattern=r"^[0-9a-f]{16}$")
    version: int = Field(ge=0)
    profile: IssuerProfile


class RetryIn(BaseModel):
    revision: int = Field(ge=1)


def service(request: Request) -> TaxTrials:
    return cast(TaxTrials, request.app.state.orchestrator.tax_trials)


def error(exc: Exception) -> HTTPException:
    return (
        HTTPException(404, "trial resource not found")
        if isinstance(exc, KeyError)
        else HTTPException(409, str(exc))
    )


@router.get("/issuers")
async def issuers(request: Request, p: Owner) -> dict[str, Any]:
    try:
        return await service(request).profiles(p.tenant)
    except TaxTrialError as exc:
        raise error(exc) from exc


@router.put("/issuers")
async def configure(body: IssuerIn, request: Request, p: Owner) -> dict[str, Any]:
    try:
        return await service(request).configure(p, body.profile, body.id, body.version)
    except (KeyError, TaxTrialError) as exc:
        raise error(exc) from exc


@router.get("/documents")
async def listing(request: Request, p: Owner) -> list[dict[str, Any]]:
    try:
        return await service(request).listing(p.tenant)
    except TaxTrialError as exc:
        raise error(exc) from exc


@router.post("/documents", status_code=201)
async def create(body: TrialRequest, request: Request, p: Owner) -> dict[str, Any]:
    try:
        return await service(request).create(p, body)
    except (KeyError, TaxTrialError) as exc:
        raise error(exc) from exc


@router.get("/documents/{document_id}")
async def detail(document_id: ID, request: Request, p: Owner) -> dict[str, Any]:
    try:
        return await service(request).get(p, document_id)
    except (KeyError, TaxTrialError) as exc:
        raise error(exc) from exc


@router.post("/documents/{document_id}/retry")
async def retry(document_id: ID, body: RetryIn, request: Request, p: Owner) -> dict[str, Any]:
    try:
        return await service(request).retry(p, document_id, body.revision)
    except (KeyError, TaxTrialError) as exc:
        raise error(exc) from exc
