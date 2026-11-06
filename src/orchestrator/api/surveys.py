"""Service feedback: own patient responses and owner-only quality reports."""

from typing import Annotated, Any, cast
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi import Path as APIPath
from pydantic import BaseModel, Field, StrictInt

from orchestrator.api.billing import no_store
from orchestrator.api.security import require_patient, requires
from orchestrator.auth import Principal, Role
from orchestrator.monthly_report import ReportError, month_bounds
from orchestrator.surveys import SurveyError, Surveys, SurveySettings, SurveySpec

router = APIRouter(tags=["service surveys"], dependencies=[Depends(no_store)])
Owner = Annotated[Principal, Depends(requires(Role.OWNER))]
Patient = Annotated[Principal, Depends(require_patient)]
ID = Annotated[str, APIPath(pattern=r"^[0-9a-f]{1,32}$")]


def surveys(request: Request) -> Surveys:
    return cast(Surveys, request.app.state.orchestrator.surveys)


def error(exc: Exception) -> HTTPException:
    return HTTPException(
        404 if isinstance(exc, KeyError) else 409,
        "survey not found" if isinstance(exc, KeyError) else str(exc),
    )


class DefinitionIn(BaseModel):
    spec: SurveySpec
    id: str | None = Field(default=None, pattern=r"^[0-9a-f]{1,32}$")
    version: int | None = Field(default=None, ge=1)


class ConfigurationIn(BaseModel):
    revision: int = Field(ge=0)
    settings: SurveySettings


class AnswersIn(BaseModel):
    answers: dict[str, StrictInt] = Field(max_length=20)


@router.get("/v1/surveys/definitions")
async def definitions(request: Request, p: Owner) -> list[dict[str, Any]]:
    return await surveys(request).definitions(p.tenant)


@router.post("/v1/surveys/definitions", status_code=201)
async def create(body: DefinitionIn, request: Request, p: Owner) -> dict[str, Any]:
    try:
        return await surveys(request).create(p.tenant, p.id, body.spec, body.id, body.version)
    except (SurveyError, KeyError) as exc:
        raise error(exc) from exc


@router.get("/v1/surveys/configuration")
async def configuration(request: Request, p: Owner) -> dict[str, Any]:
    return await surveys(request).configuration(p.tenant)


@router.put("/v1/surveys/configuration")
async def configure(body: ConfigurationIn, request: Request, p: Owner) -> dict[str, Any]:
    try:
        return await surveys(request).configure(p.tenant, p.id, body.settings, body.revision)
    except (SurveyError, KeyError) as exc:
        raise error(exc) from exc


@router.get("/v1/surveys/report")
async def report(
    request: Request, p: Owner, month: Annotated[str | None, Query(pattern=r"^\d{4}-\d{2}$")] = None
) -> dict[str, Any]:
    try:
        bounds = (
            month_bounds(month, ZoneInfo(request.app.state.settings.clinic_timezone))
            if month
            else (None, None)
        )
        return await surveys(request).report(p.tenant, *bounds)
    except ReportError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/v1/surveys/events")
async def events(request: Request, p: Owner) -> list[dict[str, Any]]:
    return await surveys(request).events(p.tenant)


@router.post("/v1/surveys/run")
async def run(request: Request, p: Owner) -> dict[str, int]:
    return await surveys(request).tick(p.tenant)


@router.post("/v1/surveys/responses/{invitation_id}/acknowledge")
async def acknowledge(invitation_id: ID, request: Request, p: Owner) -> dict[str, bool]:
    try:
        return await surveys(request).acknowledge(p.tenant, invitation_id, p.id)
    except (SurveyError, KeyError) as exc:
        raise error(exc) from exc


@router.get("/v1/me/surveys")
async def pending(request: Request, p: Patient) -> dict[str, Any]:
    assert p.subject_id is not None
    return await surveys(request).pending(p.tenant, p.subject_id)


@router.post("/v1/me/surveys/{invitation_id}")
async def answer(
    invitation_id: ID, body: AnswersIn, request: Request, p: Patient
) -> dict[str, Any]:
    assert p.subject_id is not None
    try:
        return await surveys(request).answer(
            p.tenant, p.subject_id, invitation_id, body.answers, p.id
        )
    except (SurveyError, KeyError) as exc:
        raise error(exc) from exc
