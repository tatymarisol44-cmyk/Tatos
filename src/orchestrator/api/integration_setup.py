"""Preparation only. Saving references performs no external verification or emission."""

from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from orchestrator.api.billing import no_store
from orchestrator.api.security import requires
from orchestrator.auth import Principal, Role
from orchestrator.integration_setup import (
    IntegrationSetup,
    Provider,
    SetupConflict,
    SetupProfile,
    SetupRejected,
)

router = APIRouter(
    prefix="/v1/integration-setup",
    tags=["integration preparation"],
    dependencies=[Depends(no_store)],
)
Manager = Annotated[Principal, Depends(requires(Role.ADMIN, Role.OWNER))]


class ProfileIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = Field(ge=0, le=2_000_000_000)
    profile: SetupProfile


def service(request: Request) -> IntegrationSetup:
    return cast(IntegrationSetup, request.app.state.orchestrator.integration_setup)


@router.get("")
async def listing(request: Request, p: Manager) -> list[dict[str, Any]]:
    return await service(request).listing(p.tenant)


async def save(
    request: Request, p: Principal, provider: Provider, version: int, profile: SetupProfile | None
) -> dict[str, Any]:
    try:
        return await service(request).save(p, provider, version, profile)
    except SetupConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except SetupRejected as exc:
        raise HTTPException(422, str(exc)) from exc


@router.put("/{provider}")
async def configure(
    provider: Provider, body: ProfileIn, request: Request, p: Manager
) -> dict[str, Any]:
    return await save(request, p, provider, body.version, body.profile)


@router.delete("/{provider}")
async def clear(
    provider: Provider,
    request: Request,
    p: Manager,
    version: Annotated[int, Query(ge=0, le=2_000_000_000)],
) -> dict[str, Any]:
    return await save(request, p, provider, version, None)
