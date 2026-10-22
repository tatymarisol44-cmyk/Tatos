"""Patient email/password endpoints; staff authentication remains separate."""

from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field

from orchestrator.api.security import require_patient, requires
from orchestrator.api.staff_login import Throttled
from orchestrator.auth import Principal, Role
from orchestrator.patient_login import PatientLoginError, PatientLogins

router = APIRouter(tags=["patient-account"])
Me = Annotated[Principal, Depends(require_patient)]
Reception = Annotated[Principal, Depends(requires(Role.RECEPTION))]


def _accounts(request: Request) -> PatientLogins:
    return cast(PatientLogins, request.app.state.orchestrator.patient_logins)


class Credentials(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=200)


@router.post("/v1/patient-auth/sign-in", dependencies=[Throttled])
async def sign_in(body: Credentials, request: Request) -> dict[str, str]:
    try:
        return await _accounts(request).sign_in(body.email, body.password)
    except PatientLoginError as exc:
        raise HTTPException(401, str(exc)) from exc


@router.get("/v1/me/account")
async def account(request: Request, p: Me, x_api_key: str = Header()) -> dict[str, Any]:
    return await _accounts(request).info(p, x_api_key)


@router.post("/v1/me/account", dependencies=[Throttled])
async def activate(
    body: Credentials, request: Request, p: Me, x_api_key: str = Header()
) -> dict[str, str]:
    try:
        return await _accounts(request).activate(p, x_api_key, body.email, body.password)
    except PatientLoginError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/v1/patient-auth/sign-out", status_code=204, dependencies=[Throttled])
async def sign_out(request: Request, x_api_key: str = Header(default="", max_length=200)) -> None:
    await _accounts(request).sign_out(x_api_key)


@router.post("/v1/crm/patients/{patient_id}/login-link", dependencies=[Throttled])
async def recovery(patient_id: str, request: Request, p: Reception) -> dict[str, str]:
    try:
        return await _accounts(request).recovery(p.tenant, patient_id, p.id)
    except PatientLoginError as exc:
        raise HTTPException(404, str(exc)) from exc
