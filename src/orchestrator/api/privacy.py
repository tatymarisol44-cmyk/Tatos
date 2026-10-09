"""Register of privacy cases (rights requests, breaches) with legal deadlines, for the
privacy role. The deadlines and their articles are in orchestrator/privacy_cases.py."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi import Path as FastAPIPath
from pydantic import AwareDatetime, BaseModel, Field

from orchestrator.api.security import requires
from orchestrator.auth import Principal, Role
from orchestrator.privacy_cases import KINDS, MAX_TEXT, PrivacyCaseError, PrivacyCases

router = APIRouter(prefix="/v1/privacy/cases", tags=["privacy"])
Privacy = Annotated[Principal, Depends(requires(Role.PRIVACY))]
CaseId = Annotated[str, FastAPIPath(pattern=r"^[0-9a-f]{1,32}$")]
Step = Annotated[str, FastAPIPath(pattern=r"^[a-z_]{1,32}$")]


class CaseIn(BaseModel):
    kind: Literal[KINDS]  # type: ignore[valid-type]
    summary: str = Field(
        min_length=1, max_length=MAX_TEXT, description="What happened; never clinical content"
    )
    subject_id: str | None = Field(default=None, pattern=r"^[\w.-]{1,64}$")
    opened_at: AwareDatetime | None = Field(
        default=None, description="When the request arrived or the breach became known (now)"
    )
    found_by_platform: bool = Field(
        default=False, description="Breach found by the platform: it must tell the clinic"
    )


class StepIn(BaseModel):
    outcome: str = Field(min_length=1, max_length=MAX_TEXT, description="What was done")


def service(request: Request) -> PrivacyCases:
    return request.app.state.orchestrator.privacy_cases  # type: ignore[no-any-return]


def _refused(exc: PrivacyCaseError) -> HTTPException:
    return HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc))


@router.post("", status_code=status.HTTP_201_CREATED)
async def open_case(body: CaseIn, request: Request, p: Privacy) -> dict[str, Any]:
    opened: datetime | None = body.opened_at
    try:
        return await service(request).open(
            p.tenant,
            p.id,
            kind=body.kind,
            summary=body.summary,
            subject_id=body.subject_id,
            opened_at=opened,
            found_by_platform=body.found_by_platform,
        )
    except PrivacyCaseError as exc:
        raise _refused(exc) from exc


@router.get("")
async def list_cases(
    request: Request,
    p: Privacy,
    case_status: Annotated[Literal["open", "closed"] | None, Query(alias="status")] = None,
) -> list[dict[str, Any]]:
    return await service(request).list(p.tenant, status=case_status)


@router.get("/{case_id}")
async def get_case(case_id: CaseId, request: Request, p: Privacy) -> dict[str, Any]:
    try:
        return await service(request).get(p.tenant, case_id)
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "case not found") from exc


@router.post("/{case_id}/steps/{step}")
async def complete_step(
    case_id: CaseId, step: Step, body: StepIn, request: Request, p: Privacy
) -> dict[str, Any]:
    try:
        return await service(request).complete_step(p.tenant, case_id, step, p.id, body.outcome)
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "case or step not found") from exc
    except PrivacyCaseError as exc:
        raise _refused(exc) from exc


@router.post("/{case_id}/close")
async def close_case(case_id: CaseId, request: Request, p: Privacy) -> dict[str, Any]:
    try:
        return await service(request).close(p.tenant, case_id, p.id)
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "case not found") from exc
    except PrivacyCaseError as exc:
        raise _refused(exc) from exc
