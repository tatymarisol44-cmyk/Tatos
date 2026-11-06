"""Private notice trials; no route can send a message."""

from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import AwareDatetime, BaseModel, Field

from orchestrator.api.billing import (
    Admin,
    InvoiceId,
    Reader,
    TenantId,
    no_store,
    require_operator,
    result,
)
from orchestrator.billing_notice_trials import NoticePolicy

router = APIRouter(tags=["billing notice trials"], dependencies=[Depends(no_store)])


class PolicyIn(BaseModel):
    revision: int = Field(ge=0)
    policy: NoticePolicy


class TrialIn(BaseModel):
    at: AwareDatetime


@router.get("/v1/billing/notice-trials/configuration")
async def configuration(request: Request, p: Reader) -> Any:
    return await request.app.state.orchestrator.billing_notice_trials.configuration(p.tenant)


@router.put("/v1/billing/notice-trials/configuration")
async def configure(body: PolicyIn, request: Request, p: Admin) -> Any:
    return await result(
        request.app.state.orchestrator.billing_notice_trials.configure(
            p.tenant,
            p.id,
            body.policy,
            body.revision,
        )
    )


@router.get("/v1/billing/orders/{invoice_id}/notice-trials")
async def history(invoice_id: InvoiceId, request: Request, p: Reader) -> Any:
    return await result(
        request.app.state.orchestrator.billing_notice_trials.history(p.tenant, invoice_id)
    )


@router.post("/v1/billing/orders/{invoice_id}/notice-trials")
async def simulate(invoice_id: InvoiceId, body: TrialIn, request: Request, p: Admin) -> Any:
    return await result(
        request.app.state.orchestrator.billing_notice_trials.simulate(
            p.tenant,
            invoice_id,
            p.id,
            body.at,
        )
    )


@router.get("/v1/billing/platform/notice-trials/configuration")
async def platform_configuration(request: Request, p: Admin) -> Any:
    require_operator(request, p)
    return await request.app.state.orchestrator.billing_notice_trials.configuration(p.tenant, True)


@router.put("/v1/billing/platform/notice-trials/configuration")
async def platform_configure(body: PolicyIn, request: Request, p: Admin) -> Any:
    require_operator(request, p)
    return await result(
        request.app.state.orchestrator.billing_notice_trials.configure(
            p.tenant,
            p.id,
            body.policy,
            body.revision,
            True,
        )
    )


@router.get("/v1/billing/platform/{payer_tenant}/orders/{invoice_id}/notice-trials")
async def platform_history(
    payer_tenant: TenantId, invoice_id: InvoiceId, request: Request, p: Admin
) -> Any:
    require_operator(request, p)
    return await result(
        request.app.state.orchestrator.billing_notice_trials.history(payer_tenant, invoice_id, True)
    )


@router.post("/v1/billing/platform/{payer_tenant}/orders/{invoice_id}/notice-trials")
async def platform_simulate(
    payer_tenant: TenantId, invoice_id: InvoiceId, body: TrialIn, request: Request, p: Admin
) -> Any:
    require_operator(request, p)
    return await result(
        request.app.state.orchestrator.billing_notice_trials.simulate(
            payer_tenant,
            invoice_id,
            p.id,
            body.at,
            True,
        )
    )
