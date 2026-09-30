"""Business endpoints: human reviews, data-subject rights, audit, CRM, insights and
campaigns. Every route is scoped to the caller's tenant (API key) and every write records
the acting person (X-Actor header) in the audit trail.

A record that belongs to another tenant answers exactly like a missing one (404): no
existence leak across tenants."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi import Path as FastAPIPath

from orchestrator.api.schemas import (
    SUBJECT_ID,
    AppointmentIn,
    AppointmentStatusIn,
    CampaignApproveIn,
    CampaignIn,
    CampaignTemplateIn,
    ChatResponse,
    ConsentIn,
    InsightsQuestion,
    PatientIn,
    PatientPatch,
    ReviewDecision,
    TreatmentIn,
    TreatmentStageIn,
)
from orchestrator.api.security import request_actor, require_tenant
from orchestrator.campaigns import CampaignError
from orchestrator.crm import CrmError, NotFoundError
from orchestrator.governance import Purpose
from orchestrator.insights import QuestionBlockedError
from orchestrator.service import Orchestrator, ReviewNotFoundError

router = APIRouter()


# Factories, not shared instances: FastAPI binds a Path() to the first parameter name it
# is used with, so one instance reused across routes breaks the others.
def thread_path() -> Any:
    return FastAPIPath(max_length=128, pattern=r"^[\w-]+$")


def subject_path() -> Any:
    return FastAPIPath(pattern=SUBJECT_ID)


def record_path() -> Any:
    return FastAPIPath(max_length=64, pattern=r"^[\w.-]+$")


def orch(request: Request) -> Orchestrator:
    return request.app.state.orchestrator  # type: ignore[no-any-return]


def _not_found(what: str) -> HTTPException:
    return HTTPException(status.HTTP_404_NOT_FOUND, f"{what} not found")


# --- human review (ERM) -----------------------------------------------------------
@router.get("/v1/reviews", tags=["reviews"])
async def list_reviews(
    request: Request,
    state: str = Query(default="pending", pattern=r"^(pending|approved|rejected|all)$"),
    tenant: str = Depends(require_tenant),
) -> list[dict[str, Any]]:
    """Answers paused for a human decision (or past decisions)."""
    items = await orch(request).reviews.list(tenant, None if state == "all" else state)
    return [r.to_dict() for r in items]


@router.get("/v1/reviews/{thread_id}", tags=["reviews"])
async def get_review(
    request: Request, thread_id: str = thread_path(), tenant: str = Depends(require_tenant)
) -> dict[str, Any]:
    item = await orch(request).reviews.get(tenant, thread_id)
    if item is None:
        raise _not_found("review")
    return item.to_dict()


@router.post("/v1/reviews/{thread_id}", response_model=ChatResponse, tags=["reviews"])
async def resolve_review(
    body: ReviewDecision,
    request: Request,
    thread_id: str = thread_path(),
    tenant: str = Depends(require_tenant),
    actor: str = Depends(request_actor),
) -> ChatResponse:
    """Approve (optionally editing the text) or reject a paused answer. The workflow resumes
    from its checkpoint and returns the final result."""
    try:
        result = await orch(request).resolve_review(
            tenant,
            thread_id,
            approved=body.approved,
            reviewer=actor,
            feedback=body.feedback,
            edited_answer=body.edited_answer,
        )
    except ReviewNotFoundError as exc:
        raise _not_found("pending review") from exc
    return ChatResponse(**result.__dict__)


# --- data-subject rights and consents ---------------------------------------------
@router.put("/v1/subjects/{subject_id}/consents/{purpose}", tags=["privacy"])
async def set_consent(
    body: ConsentIn,
    request: Request,
    purpose: Purpose,
    subject_id: str = subject_path(),
    tenant: str = Depends(require_tenant),
    actor: str = Depends(request_actor),
) -> dict[str, Any]:
    o = orch(request)
    await o.consents.record(
        tenant, subject_id, purpose, body.granted, source=body.source, actor=actor
    )
    return await o.consents.get(tenant, subject_id)


@router.get("/v1/subjects/{subject_id}/consents", tags=["privacy"])
async def get_consents(
    request: Request, subject_id: str = subject_path(), tenant: str = Depends(require_tenant)
) -> dict[str, Any]:
    return await orch(request).consents.get(tenant, subject_id)


@router.get("/v1/subjects/{subject_id}/export", tags=["privacy"])
async def export_subject(
    request: Request,
    subject_id: str = subject_path(),
    tenant: str = Depends(require_tenant),
    actor: str = Depends(request_actor),
) -> dict[str, Any]:
    """Right of access / portability: everything held about the subject, as JSON."""
    return await orch(request).export_subject(tenant, subject_id, actor)


@router.delete("/v1/subjects/{subject_id}", tags=["privacy"])
async def erase_subject(
    request: Request,
    subject_id: str = subject_path(),
    tenant: str = Depends(require_tenant),
    actor: str = Depends(request_actor),
) -> dict[str, Any]:
    """Right to erasure. The clinical record is retained (restricted) where the law
    requires it; conversations are erased per thread (DELETE /v1/threads/{id})."""
    return await orch(request).erase_subject(tenant, subject_id, actor)


@router.get("/v1/audit", tags=["privacy"])
async def audit_trail(
    request: Request,
    subject_id: str | None = Query(default=None, pattern=SUBJECT_ID),
    limit: int = Query(default=100, ge=1, le=1000),
    tenant: str = Depends(require_tenant),
) -> list[dict[str, Any]]:
    events = await orch(request).audit.list(tenant, subject_id=subject_id, limit=limit)
    return [e.to_dict() for e in events]


# --- CRM -------------------------------------------------------------------------
@router.post("/v1/crm/patients", status_code=status.HTTP_201_CREATED, tags=["crm"])
async def create_patient(
    body: PatientIn,
    request: Request,
    tenant: str = Depends(require_tenant),
    actor: str = Depends(request_actor),
) -> dict[str, Any]:
    try:
        return await orch(request).crm.create_patient(
            tenant, body.model_dump(exclude={"id"}), actor, patient_id=body.id
        )
    except CrmError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc


@router.get("/v1/crm/patients", tags=["crm"])
async def list_patients(
    request: Request,
    search: str | None = Query(default=None, max_length=100),
    limit: int = Query(default=100, ge=1, le=500),
    tenant: str = Depends(require_tenant),
) -> list[dict[str, Any]]:
    return await orch(request).crm.list_patients(tenant, search=search, limit=limit)


@router.get("/v1/crm/patients/{patient_id}", tags=["crm"])
async def get_patient(
    request: Request,
    patient_id: str = subject_path(),
    tenant: str = Depends(require_tenant),
    actor: str = Depends(request_actor),
) -> dict[str, Any]:
    try:
        return await orch(request).crm.get_patient(tenant, patient_id, actor=actor)
    except NotFoundError as exc:
        raise _not_found("patient") from exc


@router.patch("/v1/crm/patients/{patient_id}", tags=["crm"])
async def update_patient(
    body: PatientPatch,
    request: Request,
    patient_id: str = subject_path(),
    tenant: str = Depends(require_tenant),
    actor: str = Depends(request_actor),
) -> dict[str, Any]:
    try:
        return await orch(request).crm.update_patient(
            tenant, patient_id, body.model_dump(exclude_unset=True), actor
        )
    except NotFoundError as exc:
        raise _not_found("patient") from exc
    except CrmError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc


@router.post("/v1/crm/appointments", status_code=status.HTTP_201_CREATED, tags=["crm"])
async def create_appointment(
    body: AppointmentIn,
    request: Request,
    tenant: str = Depends(require_tenant),
    actor: str = Depends(request_actor),
) -> dict[str, Any]:
    try:
        return await orch(request).crm.create_appointment(
            tenant,
            body.patient_id,
            starts_at=body.starts_at,
            duration_min=body.duration_min,
            kind=body.kind,
            price=body.price,
            actor=actor,
        )
    except NotFoundError as exc:
        raise _not_found("patient") from exc
    except CrmError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc


@router.get("/v1/crm/appointments", tags=["crm"])
async def list_appointments(
    request: Request,
    start: datetime | None = None,
    end: datetime | None = None,
    patient_id: str | None = Query(default=None, pattern=SUBJECT_ID),
    tenant: str = Depends(require_tenant),
) -> list[dict[str, Any]]:
    return await orch(request).crm.list_appointments(
        tenant, start=start, end=end, patient_id=patient_id
    )


@router.post("/v1/crm/appointments/{appointment_id}/status", tags=["crm"])
async def set_appointment_status(
    body: AppointmentStatusIn,
    request: Request,
    appointment_id: str = record_path(),
    tenant: str = Depends(require_tenant),
    actor: str = Depends(request_actor),
) -> dict[str, Any]:
    try:
        return await orch(request).crm.set_appointment_status(
            tenant, appointment_id, body.status, actor
        )
    except NotFoundError as exc:
        raise _not_found("appointment") from exc
    except CrmError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc


@router.post("/v1/crm/treatments", status_code=status.HTTP_201_CREATED, tags=["crm"])
async def create_treatment(
    body: TreatmentIn,
    request: Request,
    tenant: str = Depends(require_tenant),
    actor: str = Depends(request_actor),
) -> dict[str, Any]:
    try:
        return await orch(request).crm.create_treatment(
            tenant, body.patient_id, title=body.title, amount=body.amount, actor=actor
        )
    except NotFoundError as exc:
        raise _not_found("patient") from exc
    except CrmError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc


@router.get("/v1/crm/treatments", tags=["crm"])
async def list_treatments(
    request: Request,
    patient_id: str | None = Query(default=None, pattern=SUBJECT_ID),
    stage: str | None = Query(default=None, max_length=16),
    tenant: str = Depends(require_tenant),
) -> list[dict[str, Any]]:
    return await orch(request).crm.list_treatments(tenant, patient_id=patient_id, stage=stage)


@router.post("/v1/crm/treatments/{treatment_id}/stage", tags=["crm"])
async def set_treatment_stage(
    body: TreatmentStageIn,
    request: Request,
    treatment_id: str = record_path(),
    tenant: str = Depends(require_tenant),
    actor: str = Depends(request_actor),
) -> dict[str, Any]:
    try:
        return await orch(request).crm.set_treatment_stage(tenant, treatment_id, body.stage, actor)
    except NotFoundError as exc:
        raise _not_found("treatment") from exc
    except CrmError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc


@router.get("/v1/crm/alerts", tags=["crm"])
async def crm_alerts(
    request: Request, tenant: str = Depends(require_tenant)
) -> list[dict[str, Any]]:
    """Traffic-light follow-ups: unconfirmed appointments, unanswered quotes, recalls."""
    return [a.to_dict() for a in await orch(request).crm.alerts(tenant)]


# --- insights --------------------------------------------------------------------
@router.get("/v1/insights/summary", tags=["insights"])
async def insights_summary(
    request: Request, tenant: str = Depends(require_tenant)
) -> dict[str, Any]:
    return await orch(request).insights.summary(tenant)


@router.get("/v1/insights/segments", tags=["insights"])
async def insights_segments(
    request: Request, tenant: str = Depends(require_tenant)
) -> dict[str, list[str]]:
    return await orch(request).insights.segments(tenant)


@router.post("/v1/insights/ask", tags=["insights"])
async def insights_ask(
    body: InsightsQuestion, request: Request, tenant: str = Depends(require_tenant)
) -> dict[str, Any]:
    """Question in natural language, answered from SQL-computed metrics only."""
    try:
        return await orch(request).insights.ask(tenant, body.question)
    except QuestionBlockedError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, {"guardrails": exc.reasons}) from exc


# --- campaigns -------------------------------------------------------------------
def _campaign_error(exc: Exception) -> HTTPException:
    if isinstance(exc, KeyError):
        return _not_found(str(exc.args[0]) if exc.args else "campaign")
    return HTTPException(status.HTTP_409_CONFLICT, str(exc))


@router.post("/v1/campaigns", status_code=status.HTTP_201_CREATED, tags=["campaigns"])
async def create_campaign(
    body: CampaignIn,
    request: Request,
    tenant: str = Depends(require_tenant),
    actor: str = Depends(request_actor),
) -> dict[str, Any]:
    try:
        return await orch(request).campaigns.create(
            tenant,
            name=body.name,
            kind=body.kind,
            segment=body.segment,
            channel=body.channel,
            template=body.template,
            holdout_pct=body.holdout_pct,
            language=body.language,
            actor=actor,
        )
    except KeyError as exc:  # unknown segment
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc.args[0])) from exc
    except CampaignError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc


@router.get("/v1/campaigns", tags=["campaigns"])
async def list_campaigns(
    request: Request, tenant: str = Depends(require_tenant)
) -> list[dict[str, Any]]:
    return await orch(request).campaigns.list_all(tenant)


@router.get("/v1/campaigns/{campaign_id}", tags=["campaigns"])
async def get_campaign(
    request: Request, campaign_id: str = record_path(), tenant: str = Depends(require_tenant)
) -> dict[str, Any]:
    try:
        return await orch(request).campaigns.get(tenant, campaign_id)
    except KeyError as exc:
        raise _not_found("campaign") from exc


@router.put("/v1/campaigns/{campaign_id}/template", tags=["campaigns"])
async def edit_campaign(
    body: CampaignTemplateIn,
    request: Request,
    campaign_id: str = record_path(),
    tenant: str = Depends(require_tenant),
    actor: str = Depends(request_actor),
) -> dict[str, Any]:
    try:
        return await orch(request).campaigns.update_template(
            tenant, campaign_id, body.template, actor
        )
    except (KeyError, CampaignError) as exc:
        raise _campaign_error(exc) from exc


@router.post("/v1/campaigns/{campaign_id}/approve", tags=["campaigns"])
async def approve_campaign(
    body: CampaignApproveIn,
    request: Request,
    campaign_id: str = record_path(),
    tenant: str = Depends(require_tenant),
    actor: str = Depends(request_actor),
) -> dict[str, Any]:
    try:
        return await orch(request).campaigns.approve(
            tenant, campaign_id, actor, owner_approval=body.owner_approval
        )
    except (KeyError, CampaignError) as exc:
        raise _campaign_error(exc) from exc


@router.post("/v1/campaigns/{campaign_id}/send", tags=["campaigns"])
async def send_campaign(
    request: Request,
    campaign_id: str = record_path(),
    tenant: str = Depends(require_tenant),
    actor: str = Depends(request_actor),
) -> dict[str, Any]:
    try:
        return await orch(request).campaigns.send(tenant, campaign_id, actor)
    except (KeyError, CampaignError) as exc:
        raise _campaign_error(exc) from exc


@router.post("/v1/campaigns/{campaign_id}/cancel", tags=["campaigns"])
async def cancel_campaign(
    request: Request,
    campaign_id: str = record_path(),
    tenant: str = Depends(require_tenant),
    actor: str = Depends(request_actor),
) -> dict[str, Any]:
    try:
        return await orch(request).campaigns.cancel(tenant, campaign_id, actor)
    except (KeyError, CampaignError) as exc:
        raise _campaign_error(exc) from exc


@router.get("/v1/campaigns/{campaign_id}/results", tags=["campaigns"])
async def campaign_results(
    request: Request, campaign_id: str = record_path(), tenant: str = Depends(require_tenant)
) -> dict[str, Any]:
    """Booking rate of the treatment arm vs the holdout, lift and significance."""
    try:
        return await orch(request).campaigns.results(tenant, campaign_id)
    except (KeyError, CampaignError) as exc:
        raise _campaign_error(exc) from exc
