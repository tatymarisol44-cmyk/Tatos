"""Billing permissions: clinic receivables versus platform subscription receivables."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi import Path as ApiPath
from pydantic import BaseModel, Field

from orchestrator.api.security import require_patient, requires
from orchestrator.auth import SERVICE, Principal, Role
from orchestrator.billing import BillingError, ChargeKind
from orchestrator.billing_store import BillingStore

router = APIRouter(tags=["billing"])
Admin = Annotated[Principal, Depends(requires(Role.ADMIN))]
Reader = Annotated[Principal, Depends(requires(Role.ADMIN, Role.OWNER, Role.RECEPTION))]
Me = Annotated[Principal, Depends(require_patient)]
InvoiceId = Annotated[str, ApiPath(pattern=r"^[0-9a-f]{32}$")]
AppointmentId = Annotated[str, ApiPath(pattern=r"^[\w.@-]{1,64}$")]
TenantId = Annotated[str, ApiPath(pattern=r"^[\w.-]{1,64}$")]


class BankIn(BaseModel):
    instructions: str = Field(min_length=10, max_length=1500)


class TransferIn(BaseModel):
    reference: str = Field(min_length=3, max_length=120)


class ReviewIn(BaseModel):
    approved: bool
    note: str = Field(min_length=5, max_length=200)
    bank_transaction_id: str | None = Field(default=None, min_length=3, max_length=120)


def store(request: Request) -> BillingStore:
    return request.app.state.orchestrator.billing  # type: ignore[no-any-return]


def operator(request: Request, p: Principal) -> bool:
    return store(request).operator(p.tenant, service=p.kind == SERVICE)


async def result(call: Any) -> Any:
    try:
        return await call
    except KeyError as exc:
        raise HTTPException(404, "payment or appointment not found") from exc
    except BillingError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.get("/v1/billing/configuration")
async def configuration(request: Request, p: Reader) -> dict[str, Any]:
    return await store(request).configuration(p.tenant, operator=operator(request, p))


@router.put("/v1/billing/bank")
async def configure_clinic(body: BankIn, request: Request, p: Admin) -> Any:
    return await result(store(request).configure_bank(p.tenant, "clinic", body.instructions, p.id))


@router.get("/v1/billing/orders")
async def listing(request: Request, p: Reader, kind: ChargeKind = ChargeKind.APPOINTMENT) -> Any:
    if kind == ChargeKind.SUBSCRIPTION and Role.ADMIN not in p.roles:
        raise HTTPException(403, "subscription billing requires administration")
    return await store(request).listing(p.tenant, kind)


@router.post("/v1/billing/appointments/{appointment_id}")
async def appointment(appointment_id: AppointmentId, request: Request, p: Reader) -> Any:
    return await result(
        store(request).create(p.tenant, ChargeKind.APPOINTMENT, appointment_id, p.id)
    )


@router.post("/v1/billing/subscription")
async def subscription(request: Request, p: Admin) -> Any:
    return await result(store(request).create(p.tenant, ChargeKind.SUBSCRIPTION, None, p.id))


@router.post("/v1/billing/orders/{invoice_id}/transfer")
async def report(invoice_id: InvoiceId, body: TransferIn, request: Request, p: Admin) -> Any:
    return await result(store(request).notify_transfer(p.tenant, invoice_id, body.reference, p.id))


@router.post("/v1/billing/orders/{invoice_id}/review")
async def review(invoice_id: InvoiceId, body: ReviewIn, request: Request, p: Admin) -> Any:
    return await result(
        store(request).review_transfer(
            p.tenant,
            invoice_id,
            p.id,
            body.approved,
            body.note,
            bank_transaction_id=body.bank_transaction_id,
        )
    )


def require_operator(request: Request, p: Principal) -> None:
    if not operator(request, p):
        raise HTTPException(403, "platform billing operator access is required")


@router.put("/v1/billing/platform/bank")
async def configure_platform(body: BankIn, request: Request, p: Admin) -> Any:
    require_operator(request, p)
    return await result(
        store(request).configure_bank(p.tenant, "platform", body.instructions, p.id, operator=True)
    )


@router.get("/v1/billing/platform/subscriptions")
async def platform_subscriptions(request: Request, p: Admin) -> Any:
    require_operator(request, p)
    return await store(request).listing(p.tenant, ChargeKind.SUBSCRIPTION, platform_review=True)


@router.post("/v1/billing/platform/{payer_tenant}/orders/{invoice_id}/review")
async def platform_review(
    payer_tenant: TenantId, invoice_id: InvoiceId, body: ReviewIn, request: Request, p: Admin
) -> Any:
    require_operator(request, p)
    return await result(
        store(request).review_transfer(
            payer_tenant,
            invoice_id,
            p.id,
            body.approved,
            body.note,
            platform_operator=True,
            bank_transaction_id=body.bank_transaction_id,
        )
    )


@router.get("/v1/me/payments")
async def patient_payments(request: Request, p: Me) -> Any:
    assert p.subject_id is not None
    return await store(request).listing(p.tenant, ChargeKind.APPOINTMENT, patient_id=p.subject_id)


@router.post("/v1/me/appointments/{appointment_id}/payment")
async def patient_invoice(appointment_id: AppointmentId, request: Request, p: Me) -> Any:
    assert p.subject_id is not None
    return await result(
        store(request).create(
            p.tenant, ChargeKind.APPOINTMENT, appointment_id, p.id, patient_id=p.subject_id
        )
    )


@router.post("/v1/me/payments/{invoice_id}/transfer")
async def patient_transfer(invoice_id: InvoiceId, body: TransferIn, request: Request, p: Me) -> Any:
    assert p.subject_id is not None
    return await result(
        store(request).notify_transfer(
            p.tenant, invoice_id, body.reference, p.id, patient_id=p.subject_id
        )
    )
