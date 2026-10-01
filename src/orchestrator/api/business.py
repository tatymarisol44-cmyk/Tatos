"""Staff endpoints: human reviews, data-subject rights, audit, CRM, insights, campaigns
and key administration.

Every route requires an authenticated staff or service key with the right role
(`auth.Role`); the acting person recorded in the audit trail is the key's principal,
never a value the caller declares. Every route is scoped to the caller's tenant, and a
record of another tenant answers exactly like a missing one (404).

Role matrix:
    reviewer   reviews
    privacy    subject export/erasure, audit trail
    reception  patients, appointments, plans, consents, patient access keys, alerts
    owner      insights; campaigns, including discounts above the pack's cap
    marketing  campaigns (create, edit, approve within the cap, send, results)
    admin      staff keys, documents; implies every role"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi import Path as FastAPIPath

from orchestrator.api.schemas import (
    SUBJECT_ID,
    AppointmentIn,
    AppointmentStatusIn,
    CampaignIn,
    CampaignTemplateIn,
    ChatResponse,
    ConsentIn,
    InsightsQuestion,
    PatientIn,
    PatientPatch,
    ReviewDecision,
    StaffKeyIn,
    TreatmentIn,
    TreatmentStageIn,
)
from orchestrator.api.security import requires
from orchestrator.auth import Principal, Role
from orchestrator.campaigns import CampaignError
from orchestrator.crm import CrmError, NotFoundError
from orchestrator.governance import Purpose
from orchestrator.insights import QuestionBlockedError
from orchestrator.service import Orchestrator, ReviewNotFoundError

router = APIRouter()

Reviewer = Annotated[Principal, Depends(requires(Role.REVIEWER))]
Privacy = Annotated[Principal, Depends(requires(Role.PRIVACY))]
Reception = Annotated[Principal, Depends(requires(Role.RECEPTION))]
# Care staff who may read the CRM without changing it.
CareReader = Annotated[Principal, Depends(requires(Role.RECEPTION, Role.REVIEWER, Role.OWNER))]
Owner = Annotated[Principal, Depends(requires(Role.OWNER))]
Marketing = Annotated[Principal, Depends(requires(Role.MARKETING, Role.OWNER))]
Admin = Annotated[Principal, Depends(requires(Role.ADMIN))]
ConsentStaff = Annotated[Principal, Depends(requires(Role.RECEPTION, Role.PRIVACY))]


# Factories, not shared instances: FastAPI binds a Path() to the first parameter name it
# is used with, so one instance reused across routes breaks the others.
def thread_path() -> Any:
    return FastAPIPath(max_length=128, pattern=r"^[\w-]+$")


def subject_path() -> Any:
    return FastAPIPath(pattern=SUBJECT_ID)


def record_path() -> Any:
    return FastAPIPath(max_length=128, pattern=r"^[\w.:-]+$")


def orch(request: Request) -> Orchestrator:
    return request.app.state.orchestrator  # type: ignore[no-any-return]


def _not_found(what: str) -> HTTPException:
    return HTTPException(status.HTTP_404_NOT_FOUND, f"{what} not found")


# --- human review (ERM) -----------------------------------------------------------
@router.get("/v1/reviews", tags=["reviews"])
async def list_reviews(
    request: Request,
    p: Reviewer,
    state: str = Query(default="pending", pattern=r"^(pending|approved|rejected|all)$"),
) -> list[dict[str, Any]]:
    """Answers paused for a human decision (or past decisions)."""
    items = await orch(request).reviews.list(p.tenant, None if state == "all" else state)
    return [r.to_dict() for r in items]


@router.get("/v1/reviews/{thread_id}", tags=["reviews"])
async def get_review(
    request: Request, thread_id: Annotated[str, thread_path()], p: Reviewer
) -> dict[str, Any]:
    item = await orch(request).reviews.get(p.tenant, thread_id)
    if item is None:
        raise _not_found("review")
    return item.to_dict()


@router.post("/v1/reviews/{thread_id}", response_model=ChatResponse, tags=["reviews"])
async def resolve_review(
    body: ReviewDecision, request: Request, thread_id: Annotated[str, thread_path()], p: Reviewer
) -> ChatResponse:
    """Approve (optionally editing the text) or reject a paused answer. The workflow resumes
    from its checkpoint and returns the final result. The reviewer is the key's owner."""
    try:
        result = await orch(request).resolve_review(
            p.tenant,
            thread_id,
            approved=body.approved,
            reviewer=p.id,
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
    p: ConsentStaff,
    purpose: Purpose,
    subject_id: Annotated[str, subject_path()],
) -> dict[str, Any]:
    o = orch(request)
    await o.consents.record(
        p.tenant, subject_id, purpose, body.granted, source=body.source, actor=p.id
    )
    return await o.consents.get(p.tenant, subject_id)


@router.get("/v1/subjects/{subject_id}/consents", tags=["privacy"])
async def get_consents(
    request: Request,
    p: ConsentStaff,
    subject_id: Annotated[str, subject_path()],
) -> dict[str, Any]:
    return await orch(request).consents.get(p.tenant, subject_id)


@router.get("/v1/subjects/{subject_id}/export", tags=["privacy"])
async def export_subject(
    request: Request, subject_id: Annotated[str, subject_path()], p: Privacy
) -> dict[str, Any]:
    """Right of access / portability: the subject's data, as JSON."""
    return await orch(request).export_subject(p.tenant, subject_id, p.id)


@router.delete("/v1/subjects/{subject_id}", tags=["privacy"])
async def erase_subject(
    request: Request, subject_id: Annotated[str, subject_path()], p: Privacy
) -> dict[str, Any]:
    """Right to erasure. The clinical record is retained (restricted) where the law
    requires it; conversations are erased per thread (DELETE /v1/threads/{id})."""
    return await orch(request).erase_subject(p.tenant, subject_id, p.id)


@router.get("/v1/audit", tags=["privacy"])
async def audit_trail(
    request: Request,
    p: Privacy,
    subject_id: str | None = Query(default=None, pattern=SUBJECT_ID),
    limit: int = Query(default=100, ge=1, le=1000),
) -> list[dict[str, Any]]:
    events = await orch(request).audit.list(p.tenant, subject_id=subject_id, limit=limit)
    return [e.to_dict() for e in events]


# --- key administration -------------------------------------------------------------
@router.post("/v1/admin/staff", status_code=status.HTTP_201_CREATED, tags=["admin"])
async def create_staff_key(body: StaffKeyIn, request: Request, p: Admin) -> dict[str, Any]:
    """Create a personal key for a staff member. The key is shown only in this response."""
    try:
        info, key = await orch(request).principals.create_staff(
            p.tenant, body.name, body.roles, p.id
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return {**info, "key": key}


@router.get("/v1/admin/principals", tags=["admin"])
async def list_principals(request: Request, p: Admin) -> list[dict[str, Any]]:
    return await orch(request).principals.list(p.tenant)


@router.delete("/v1/admin/principals/{principal_id}", status_code=204, tags=["admin"])
async def revoke_principal(
    request: Request, principal_id: Annotated[str, record_path()], p: Admin
) -> None:
    if not await orch(request).principals.revoke(p.tenant, principal_id, p.id):
        raise _not_found("active key")


@router.post(
    "/v1/crm/patients/{patient_id}/access",
    status_code=status.HTTP_201_CREATED,
    tags=["crm"],
)
async def create_patient_access(
    request: Request, patient_id: Annotated[str, subject_path()], p: Reception
) -> dict[str, Any]:
    """Issue a personal key for the patient (to send as a link). It only opens /v1/me,
    scoped to this patient, and expires after PATIENT_ACCESS_TTL_DAYS."""
    o = orch(request)
    try:
        patient = await o.crm.get_patient(p.tenant, patient_id, actor=None)
    except NotFoundError as exc:
        raise _not_found("patient") from exc
    if patient["restricted"]:
        raise HTTPException(status.HTTP_409_CONFLICT, "patient record is restricted")
    info, key = await o.principals.create_patient_access(
        p.tenant, patient_id, p.id, o.settings.patient_access_ttl_days
    )
    return {**info, "key": key}


# --- CRM -------------------------------------------------------------------------
@router.post("/v1/crm/patients", status_code=status.HTTP_201_CREATED, tags=["crm"])
async def create_patient(body: PatientIn, request: Request, p: Reception) -> dict[str, Any]:
    try:
        return await orch(request).crm.create_patient(
            p.tenant, body.model_dump(exclude={"id"}), p.id, patient_id=body.id
        )
    except CrmError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc


@router.get("/v1/crm/patients", tags=["crm"])
async def list_patients(
    request: Request,
    p: CareReader,
    search: str | None = Query(default=None, max_length=100),
    limit: int = Query(default=100, ge=1, le=500),
) -> list[dict[str, Any]]:
    return await orch(request).crm.list_patients(p.tenant, search=search, limit=limit)


@router.get("/v1/crm/patients/{patient_id}", tags=["crm"])
async def get_patient(
    request: Request, patient_id: Annotated[str, subject_path()], p: CareReader
) -> dict[str, Any]:
    try:
        return await orch(request).crm.get_patient(p.tenant, patient_id, actor=p.id)
    except NotFoundError as exc:
        raise _not_found("patient") from exc


@router.patch("/v1/crm/patients/{patient_id}", tags=["crm"])
async def update_patient(
    body: PatientPatch, request: Request, patient_id: Annotated[str, subject_path()], p: Reception
) -> dict[str, Any]:
    try:
        return await orch(request).crm.update_patient(
            p.tenant, patient_id, body.model_dump(exclude_unset=True), p.id
        )
    except NotFoundError as exc:
        raise _not_found("patient") from exc
    except CrmError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc


@router.post("/v1/crm/appointments", status_code=status.HTTP_201_CREATED, tags=["crm"])
async def create_appointment(body: AppointmentIn, request: Request, p: Reception) -> dict[str, Any]:
    try:
        return await orch(request).crm.create_appointment(
            p.tenant,
            body.patient_id,
            starts_at=body.starts_at,
            duration_min=body.duration_min,
            kind=body.kind,
            price=body.price,
            actor=p.id,
        )
    except NotFoundError as exc:
        raise _not_found("patient") from exc
    except CrmError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc


@router.get("/v1/crm/appointments", tags=["crm"])
async def list_appointments(
    request: Request,
    p: CareReader,
    start: datetime | None = None,
    end: datetime | None = None,
    patient_id: str | None = Query(default=None, pattern=SUBJECT_ID),
) -> list[dict[str, Any]]:
    return await orch(request).crm.list_appointments(
        p.tenant, start=start, end=end, patient_id=patient_id
    )


@router.post("/v1/crm/appointments/{appointment_id}/status", tags=["crm"])
async def set_appointment_status(
    body: AppointmentStatusIn,
    request: Request,
    p: Reception,
    appointment_id: Annotated[str, record_path()],
) -> dict[str, Any]:
    try:
        return await orch(request).crm.set_appointment_status(
            p.tenant, appointment_id, body.status, p.id
        )
    except NotFoundError as exc:
        raise _not_found("appointment") from exc
    except CrmError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc


@router.post("/v1/crm/treatments", status_code=status.HTTP_201_CREATED, tags=["crm"])
async def create_treatment(body: TreatmentIn, request: Request, p: Reception) -> dict[str, Any]:
    try:
        return await orch(request).crm.create_treatment(
            p.tenant, body.patient_id, title=body.title, amount=body.amount, actor=p.id
        )
    except NotFoundError as exc:
        raise _not_found("patient") from exc
    except CrmError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc


@router.get("/v1/crm/treatments", tags=["crm"])
async def list_treatments(
    request: Request,
    p: CareReader,
    patient_id: str | None = Query(default=None, pattern=SUBJECT_ID),
    stage: str | None = Query(default=None, max_length=16),
) -> list[dict[str, Any]]:
    return await orch(request).crm.list_treatments(p.tenant, patient_id=patient_id, stage=stage)


@router.post("/v1/crm/treatments/{treatment_id}/stage", tags=["crm"])
async def set_treatment_stage(
    body: TreatmentStageIn,
    request: Request,
    p: Reception,
    treatment_id: Annotated[str, record_path()],
) -> dict[str, Any]:
    try:
        return await orch(request).crm.set_treatment_stage(p.tenant, treatment_id, body.stage, p.id)
    except NotFoundError as exc:
        raise _not_found("treatment") from exc
    except CrmError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc


@router.get("/v1/crm/alerts", tags=["crm"])
async def crm_alerts(request: Request, p: CareReader) -> list[dict[str, Any]]:
    """Traffic-light follow-ups: unconfirmed appointments, unanswered quotes, recalls."""
    return [a.to_dict() for a in await orch(request).crm.alerts(p.tenant)]


# --- insights --------------------------------------------------------------------
@router.get("/v1/insights/summary", tags=["insights"])
async def insights_summary(request: Request, p: Owner) -> dict[str, Any]:
    return await orch(request).insights.summary(p.tenant)


@router.get("/v1/insights/segments", tags=["insights"])
async def insights_segments(request: Request, p: Marketing) -> dict[str, list[str]]:
    return await orch(request).insights.segments(p.tenant)


@router.post("/v1/insights/ask", tags=["insights"])
async def insights_ask(body: InsightsQuestion, request: Request, p: Owner) -> dict[str, Any]:
    """Question in natural language, answered from SQL-computed metrics only."""
    try:
        return await orch(request).insights.ask(p.tenant, body.question)
    except QuestionBlockedError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, {"guardrails": exc.reasons}) from exc


# --- campaigns -------------------------------------------------------------------
def _campaign_error(exc: Exception) -> HTTPException:
    if isinstance(exc, KeyError):
        return _not_found(str(exc.args[0]) if exc.args else "campaign")
    return HTTPException(status.HTTP_409_CONFLICT, str(exc))


@router.post("/v1/campaigns", status_code=status.HTTP_201_CREATED, tags=["campaigns"])
async def create_campaign(body: CampaignIn, request: Request, p: Marketing) -> dict[str, Any]:
    try:
        return await orch(request).campaigns.create(
            p.tenant,
            name=body.name,
            kind=body.kind,
            segment=body.segment,
            channel=body.channel,
            template=body.template,
            holdout_pct=body.holdout_pct,
            language=body.language,
            actor=p.id,
        )
    except KeyError as exc:  # unknown segment
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc.args[0])) from exc
    except CampaignError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc


@router.get("/v1/campaigns", tags=["campaigns"])
async def list_campaigns(request: Request, p: Marketing) -> list[dict[str, Any]]:
    return await orch(request).campaigns.list_all(p.tenant)


@router.get("/v1/campaigns/{campaign_id}", tags=["campaigns"])
async def get_campaign(
    request: Request, campaign_id: Annotated[str, record_path()], p: Marketing
) -> dict[str, Any]:
    try:
        return await orch(request).campaigns.get(p.tenant, campaign_id)
    except KeyError as exc:
        raise _not_found("campaign") from exc


@router.put("/v1/campaigns/{campaign_id}/template", tags=["campaigns"])
async def edit_campaign(
    body: CampaignTemplateIn,
    request: Request,
    p: Marketing,
    campaign_id: Annotated[str, record_path()],
) -> dict[str, Any]:
    try:
        return await orch(request).campaigns.update_template(
            p.tenant, campaign_id, body.template, p.id
        )
    except (KeyError, CampaignError) as exc:
        raise _campaign_error(exc) from exc


@router.post("/v1/campaigns/{campaign_id}/approve", tags=["campaigns"])
async def approve_campaign(
    request: Request, campaign_id: Annotated[str, record_path()], p: Marketing
) -> dict[str, Any]:
    """Approve the copy. A discount above the pack's cap needs the owner role: the owner's
    approval comes from the key, it cannot be asserted in the request."""
    try:
        return await orch(request).campaigns.approve(
            p.tenant, campaign_id, p.id, owner_approval=p.has(Role.OWNER)
        )
    except (KeyError, CampaignError) as exc:
        raise _campaign_error(exc) from exc


@router.post("/v1/campaigns/{campaign_id}/send", tags=["campaigns"])
async def send_campaign(
    request: Request, campaign_id: Annotated[str, record_path()], p: Marketing
) -> dict[str, Any]:
    try:
        return await orch(request).campaigns.send(p.tenant, campaign_id, p.id)
    except (KeyError, CampaignError) as exc:
        raise _campaign_error(exc) from exc


@router.post("/v1/campaigns/{campaign_id}/cancel", tags=["campaigns"])
async def cancel_campaign(
    request: Request, campaign_id: Annotated[str, record_path()], p: Marketing
) -> dict[str, Any]:
    try:
        return await orch(request).campaigns.cancel(p.tenant, campaign_id, p.id)
    except (KeyError, CampaignError) as exc:
        raise _campaign_error(exc) from exc


@router.get("/v1/campaigns/{campaign_id}/results", tags=["campaigns"])
async def campaign_results(
    request: Request, campaign_id: Annotated[str, record_path()], p: Marketing
) -> dict[str, Any]:
    """Booking rate of the treatment arm vs the holdout, lift and significance."""
    try:
        return await orch(request).campaigns.results(p.tenant, campaign_id)
    except (KeyError, CampaignError) as exc:
        raise _campaign_error(exc) from exc
