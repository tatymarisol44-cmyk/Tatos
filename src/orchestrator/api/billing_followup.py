"""Scoped manual WhatsApp follow-up; no endpoint sends messages to a provider."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi import Path as ApiPath
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
from orchestrator.auth import Role
from orchestrator.billing_followup import PaymentFollowups

router = APIRouter(tags=["billing"], dependencies=[Depends(no_store)])
EventId = Annotated[str, ApiPath(pattern=r"^[0-9a-f]{32}$")]


class ContactIn(BaseModel):
    phone: str | None = Field(default=None, min_length=8, max_length=32)
    whatsapp_allowed: bool = False
    source: str = Field(min_length=5, max_length=128)


class ScheduleIn(BaseModel):
    next_contact_at: datetime | None = None
    promised_for: datetime | None = None
    paused: bool = False


class CompletionIn(BaseModel):
    sent: bool


def followups(request: Request) -> PaymentFollowups:
    return request.app.state.orchestrator.payment_followups  # type: ignore[no-any-return]


@router.get("/v1/billing/contact")
async def contact(request: Request, p: Admin) -> Any:
    return await followups(request).contact(p.tenant)


@router.put("/v1/billing/contact")
async def configure_contact(body: ContactIn, request: Request, p: Admin) -> Any:
    return await result(
        followups(request).configure_contact(
            p.tenant,
            body.phone,
            body.whatsapp_allowed,
            body.source,
            p.id,
        )
    )


@router.get("/v1/billing/followups")
async def clinic_followups(request: Request, p: Reader, after: str = "") -> Any:
    data = await result(followups(request).listing(p.tenant, after=after))
    return {**data, "can_record_permission": p.has(Role.RECEPTION, Role.PRIVACY)}


@router.put("/v1/billing/orders/{invoice_id}/followup")
async def clinic_schedule(
    invoice_id: InvoiceId, body: ScheduleIn, request: Request, p: Reader
) -> Any:
    return await result(
        followups(request).schedule(
            p.tenant,
            invoice_id,
            p.id,
            **body.model_dump(),
        )
    )


@router.get("/v1/billing/orders/{invoice_id}/followup")
async def clinic_history(invoice_id: InvoiceId, request: Request, p: Reader) -> Any:
    data = await result(followups(request).get(p.tenant, invoice_id))
    return {**data, "can_record_permission": p.has(Role.RECEPTION, Role.PRIVACY)}


@router.post("/v1/billing/orders/{invoice_id}/whatsapp")
async def clinic_prepare(invoice_id: InvoiceId, request: Request, p: Reader) -> Any:
    return await result(followups(request).prepare(p.tenant, invoice_id, p.id))


@router.post("/v1/billing/orders/{invoice_id}/whatsapp/{event_id}")
async def clinic_complete(
    invoice_id: InvoiceId,
    event_id: EventId,
    body: CompletionIn,
    request: Request,
    p: Reader,
) -> Any:
    return await result(
        followups(request).complete(p.tenant, invoice_id, event_id, p.id, body.sent)
    )


@router.get("/v1/billing/platform/followups")
async def platform_followups(request: Request, p: Admin, after: str = "") -> Any:
    require_operator(request, p)
    return await result(followups(request).listing(p.tenant, platform=True, after=after))


@router.put("/v1/billing/platform/{payer_tenant}/orders/{invoice_id}/followup")
async def platform_schedule(
    payer_tenant: TenantId, invoice_id: InvoiceId, body: ScheduleIn, request: Request, p: Admin
) -> Any:
    require_operator(request, p)
    return await result(
        followups(request).schedule(
            payer_tenant,
            invoice_id,
            p.id,
            platform=True,
            **body.model_dump(),
        )
    )


@router.get("/v1/billing/platform/{payer_tenant}/orders/{invoice_id}/followup")
async def platform_history(
    payer_tenant: TenantId, invoice_id: InvoiceId, request: Request, p: Admin
) -> Any:
    require_operator(request, p)
    return await result(followups(request).get(payer_tenant, invoice_id, platform=True))


@router.post("/v1/billing/platform/{payer_tenant}/orders/{invoice_id}/whatsapp")
async def platform_prepare(
    payer_tenant: TenantId, invoice_id: InvoiceId, request: Request, p: Admin
) -> Any:
    require_operator(request, p)
    return await result(followups(request).prepare(payer_tenant, invoice_id, p.id, platform=True))


@router.post("/v1/billing/platform/{payer_tenant}/orders/{invoice_id}/whatsapp/{event_id}")
async def platform_complete(
    payer_tenant: TenantId,
    invoice_id: InvoiceId,
    event_id: EventId,
    body: CompletionIn,
    request: Request,
    p: Admin,
) -> Any:
    require_operator(request, p)
    return await result(
        followups(request).complete(
            payer_tenant,
            invoice_id,
            event_id,
            p.id,
            body.sent,
            platform=True,
        )
    )
