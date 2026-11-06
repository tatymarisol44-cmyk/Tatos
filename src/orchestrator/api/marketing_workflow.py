"""Tenant marketing identity and reviewable plans using the existing catalog."""

from datetime import date
from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, File, HTTPException, Request, Response, UploadFile
from fastapi import Path as APIPath
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from orchestrator.api.security import requires
from orchestrator.auth import Principal, Role
from orchestrator.creatives import CreativeRejected, CreativeUnavailable, Format
from orchestrator.governance import ThreadBusyError
from orchestrator.marketing_designer import (
    DesignBrief,
    DesignError,
    DesignerUnavailable,
    MarketingDraft,
)
from orchestrator.marketing_workflow import Brand, CampaignPlan, WorkflowConflict
from orchestrator.publishing import PublicationError
from orchestrator.service import Orchestrator

router = APIRouter(prefix="/v1/marketing", tags=["marketing workflow"])
Marketing = Annotated[Principal, Depends(requires(Role.MARKETING, Role.OWNER))]
ID = Annotated[str, APIPath(pattern=r"^[0-9a-f]{1,32}$")]


def orch(request: Request) -> Orchestrator:
    return cast(Orchestrator, request.app.state.orchestrator)


class BrandIn(BaseModel):
    version: int = Field(ge=0)
    brand: Brand


class PlanIn(BaseModel):
    brief: DesignBrief
    first: date


class EditIn(BaseModel):
    revision: int = Field(ge=1)
    draft: MarketingDraft
    plan: CampaignPlan


class RenderIn(BaseModel):
    revision: int = Field(ge=1)
    account_id: str = Field(pattern=r"^[0-9a-f]{1,32}$")
    formats: list[Format] = Field(default=["feed"], min_length=1, max_length=3)


class RevisionIn(BaseModel):
    revision: int = Field(ge=1)


HANDLED = (
    KeyError,
    DesignError,
    DesignerUnavailable,
    WorkflowConflict,
    PublicationError,
    CreativeRejected,
    CreativeUnavailable,
    ThreadBusyError,
)


def error(exc: Exception) -> HTTPException:
    if isinstance(exc, KeyError):
        return HTTPException(404, "resource not found")
    if isinstance(exc, (DesignerUnavailable, CreativeUnavailable)):
        return HTTPException(503, str(exc))
    return HTTPException(409, str(exc))


@router.get("/brand")
async def get_brand(request: Request, p: Marketing) -> dict[str, Any]:
    return await orch(request).branding.get(p.tenant)


@router.put("/brand")
async def save_brand(body: BrandIn, request: Request, p: Marketing) -> dict[str, Any]:
    try:
        return await orch(request).branding.save(p.tenant, p.id, body.brand, body.version)
    except HANDLED as exc:
        raise error(exc) from exc


@router.put("/brand/logo")
async def upload_logo(
    request: Request, p: Marketing, version: int, file: Annotated[UploadFile, File()]
) -> dict[str, Any]:
    data = await file.read(2_000_001)
    try:
        return await orch(request).branding.set_logo(p.tenant, p.id, data, version)
    except HANDLED as exc:
        raise error(exc) from exc


@router.get("/brand/logo")
async def get_logo(request: Request, p: Marketing) -> Response:
    data = await orch(request).branding.logo(p.tenant)
    if data is None:
        raise HTTPException(404, "logo not found")
    return Response(data, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@router.get("/workflows")
async def list_flows(request: Request, p: Marketing) -> list[dict[str, Any]]:
    return await orch(request).marketing_workflow.list_flows(p.tenant)


@router.post("/workflows", status_code=201)
async def new_flow(body: PlanIn, request: Request, p: Marketing) -> dict[str, Any]:
    try:
        return await orch(request).marketing_workflow.create(p.tenant, p.id, body.brief, body.first)
    except HANDLED as exc:
        raise error(exc) from exc


@router.get("/workflows/{flow_id}")
async def get_flow(flow_id: ID, request: Request, p: Marketing) -> dict[str, Any]:
    try:
        return await orch(request).marketing_workflow.get(p.tenant, flow_id)
    except HANDLED as exc:
        raise error(exc) from exc


@router.put("/workflows/{flow_id}")
async def edit_flow(flow_id: ID, body: EditIn, request: Request, p: Marketing) -> dict[str, Any]:
    try:
        return await orch(request).marketing_workflow.edit(
            p.tenant,
            p.id,
            flow_id,
            body.revision,
            body.draft,
            body.plan,
        )
    except HANDLED as exc:
        raise error(exc) from exc


@router.post("/workflows/{flow_id}/render")
async def render_flow(
    flow_id: ID, body: RenderIn, request: Request, p: Marketing
) -> dict[str, Any]:
    try:
        return await orch(request).marketing_workflow.render(
            p.tenant,
            p.id,
            flow_id,
            body.revision,
            body.account_id,
            body.formats,
        )
    except HANDLED as exc:
        raise error(exc) from exc


@router.get("/workflows/{flow_id}/analysis")
async def analyze(flow_id: ID, request: Request, p: Marketing) -> dict[str, Any]:
    try:
        return await orch(request).marketing_workflow.analysis(p.tenant, flow_id)
    except HANDLED as exc:
        raise error(exc) from exc


@router.get("/creative/{publication_id}")
async def creative(publication_id: ID, request: Request, p: Marketing) -> FileResponse:
    service = orch(request).publications
    try:
        resource = await service.get(p.tenant, publication_id)
        path = await service.store.local_file(resource["object_name"])
        if path is None:
            raise KeyError(publication_id)
        return FileResponse(
            path,
            media_type="image/jpeg" if resource["media_format"] == "jpeg" else "video/mp4",
            headers={"Cache-Control": "no-store"},
        )
    except KeyError as exc:
        raise error(exc) from exc


@router.post("/workflows/{flow_id}/optimize")
async def optimize(flow_id: ID, body: RevisionIn, request: Request, p: Marketing) -> dict[str, Any]:
    try:
        return await orch(request).marketing_workflow.optimize(
            p.tenant, p.id, flow_id, body.revision
        )
    except HANDLED as exc:
        raise error(exc) from exc
