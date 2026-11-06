"""Explicit administration of automatic payment reminders, never a send-now endpoint."""

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from orchestrator.api.billing import (
    Admin,
    InvoiceId,
    Reader,
    TenantId,
    no_store,
    require_operator,
    result,
)

router = APIRouter(tags=["billing"], dependencies=[Depends(no_store)])


class PolicyIn(BaseModel):
    enabled: bool = False
    account_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")
    template_name: str = Field(default="", pattern=r"^$|^[a-z][a-z0-9_]{2,127}$")
    language: str = Field(default="es", pattern=r"^[a-z]{2}(?:_[A-Z]{2})?$")
    cadence_days: int = Field(default=1, ge=1, le=30)
    maximum: int = Field(default=3, ge=1, le=3)


class TargetIn(BaseModel):
    enabled: bool = False
    due_at: datetime | None = None


@router.get("/v1/billing/automation")
async def clinic_policy(request: Request, p: Admin) -> Any:
    return await request.app.state.orchestrator.billing_automation.policy(p.tenant)


@router.put("/v1/billing/automation")
async def configure_clinic(body: PolicyIn, request: Request, p: Admin) -> Any:
    return await result(
        request.app.state.orchestrator.billing_automation.configure_policy(
            p.tenant,
            p.id,
            **body.model_dump(),
        )
    )


@router.get("/v1/billing/platform/automation")
async def platform_policy(request: Request, p: Admin) -> Any:
    require_operator(request, p)
    return await request.app.state.orchestrator.billing_automation.policy(p.tenant, True)


@router.put("/v1/billing/platform/automation")
async def configure_platform(body: PolicyIn, request: Request, p: Admin) -> Any:
    require_operator(request, p)
    return await result(
        request.app.state.orchestrator.billing_automation.configure_policy(
            p.tenant,
            p.id,
            platform=True,
            **body.model_dump(),
        )
    )


@router.put("/v1/billing/orders/{invoice_id}/automation")
async def configure_invoice(
    invoice_id: InvoiceId, body: TargetIn, request: Request, p: Reader
) -> Any:
    return await result(
        request.app.state.orchestrator.billing_automation.configure_order(
            p.tenant,
            invoice_id,
            body.enabled,
            body.due_at,
            p.id,
        )
    )


@router.put("/v1/billing/platform/{payer_tenant}/automation/{execution_id}")
async def configure_renewal(
    payer_tenant: TenantId, execution_id: str, body: TargetIn, request: Request, p: Admin
) -> Any:
    require_operator(request, p)
    service = request.app.state.orchestrator.billing_automation
    key = "renewal:" + execution_id
    if await service.target(payer_tenant, key) is None:
        from fastapi import HTTPException

        raise HTTPException(404, "automatic reminder not found")
    return await result(
        service.configure_target(
            payer_tenant, key, "renewal", execution_id, body.enabled, body.due_at, p.id
        )
    )
