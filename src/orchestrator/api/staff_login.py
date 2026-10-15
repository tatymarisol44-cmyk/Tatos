"""Staff sign-in with e-mail, password and an e-mailed code (ADR 0024), and the team as the
administration manages it.

- Public, no key: `/v1/auth/options`, `sign-in`, `code`, `resend`, `forgot`, `password`.
  Rate-limited per client address (AUTH_RATE_PER_MINUTE) on top of the per-login limits
  of staff_login.py; their answers never tell whether an e-mail exists.
- Signed in: `/v1/auth/sign-out` ends the session the request came with.
- Administration (role admin): `/v1/admin/team` lists, adds, re-invites and removes."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from fastapi import Path as FastAPIPath
from pydantic import BaseModel, Field

from orchestrator.api.security import require_staff, requires
from orchestrator.auth import SESSION_PREFIX, Principal, Role
from orchestrator.staff_login import (
    LinkError,
    MailUnavailable,
    SignInError,
    TooSoon,
    WeakPassword,
)

router = APIRouter(tags=["auth"])
Admin = Annotated[Principal, Depends(requires(Role.ADMIN))]
Staff = Annotated[Principal, Depends(require_staff)]
MemberId = Annotated[str, FastAPIPath(pattern=r"^[\w.@:-]{1,128}$")]
WRONG = "wrong e-mail, password or code"


class SignInIn(BaseModel):
    email: str = Field(min_length=1, max_length=254)
    password: str = Field(min_length=1, max_length=200)


class CodeIn(BaseModel):
    challenge: str = Field(pattern=r"^[0-9a-f]{32}$")
    code: str = Field(pattern=r"^\s*\d{6}\s*$")
    remember: bool = False


class ResendIn(BaseModel):
    challenge: str = Field(pattern=r"^[0-9a-f]{32}$")


class ForgotIn(BaseModel):
    email: str = Field(min_length=1, max_length=254)


class PasswordIn(BaseModel):
    token: str = Field(min_length=20, max_length=100)
    password: str = Field(min_length=1, max_length=200)


class MemberIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    email: str = Field(min_length=3, max_length=254)
    roles: list[Role] = Field(min_length=1)


async def _throttle(request: Request) -> None:
    limiter = request.app.state.auth_limiter
    host = request.client.host if request.client else "unknown"
    if not await limiter.allow(f"auth:{host}"):
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "too many attempts: wait a minute",
            headers={"Retry-After": "60"},
        )


Throttled = Depends(_throttle)


def _logins(request: Request) -> Any:
    return request.app.state.orchestrator.staff_logins


@router.get("/v1/auth/options")
async def options(request: Request) -> dict[str, bool]:
    """What the sign-in screen offers: e-mail sign-in needs a way to send e-mail."""
    return {"email": request.app.state.orchestrator.mailer.available}


@router.post("/v1/auth/sign-in", dependencies=[Throttled])
async def sign_in(body: SignInIn, request: Request) -> dict[str, str]:
    try:
        return await _logins(request).begin(body.email, body.password)  # type: ignore[no-any-return]
    except SignInError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, WRONG) from exc
    except MailUnavailable as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "the code could not be sent by e-mail"
        ) from exc


@router.post("/v1/auth/code", dependencies=[Throttled])
async def code(body: CodeIn, request: Request) -> dict[str, str]:
    try:
        return await _logins(request).finish(body.challenge, body.code, body.remember)  # type: ignore[no-any-return]
    except SignInError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, WRONG) from exc


@router.post("/v1/auth/resend", status_code=status.HTTP_204_NO_CONTENT, dependencies=[Throttled])
async def resend(body: ResendIn, request: Request) -> None:
    try:
        await _logins(request).resend(body.challenge)
    except SignInError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, WRONG) from exc
    except TooSoon as exc:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "a code was sent less than a minute ago, or too many were sent",
        ) from exc
    except MailUnavailable as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "the code could not be sent by e-mail"
        ) from exc


@router.post("/v1/auth/forgot", status_code=status.HTTP_202_ACCEPTED, dependencies=[Throttled])
async def forgot(body: ForgotIn, request: Request) -> dict[str, str]:
    await _logins(request).forgot(body.email)
    return {"status": "if this e-mail signs in here, a link to choose a password was sent"}


@router.post("/v1/auth/password", status_code=status.HTTP_204_NO_CONTENT, dependencies=[Throttled])
async def choose_password(body: PasswordIn, request: Request) -> None:
    try:
        await _logins(request).set_password(body.token, body.password)
    except LinkError as exc:
        raise HTTPException(status.HTTP_410_GONE, "this link was used or expired") from exc
    except WeakPassword as exc:
        # Already in the person's language: the app shows it as it comes.
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc


@router.post("/v1/auth/sign-out", status_code=status.HTTP_204_NO_CONTENT)
async def sign_out(
    request: Request, p: Staff, x_api_key: str | None = Header(default=None)
) -> None:
    """Ends the e-mail session this request came with (a staff key is not affected)."""
    if x_api_key and x_api_key.startswith(SESSION_PREFIX):
        await request.app.state.orchestrator.principals.end_session(x_api_key)


@router.get("/v1/admin/team", tags=["admin"])
async def team(request: Request, p: Admin) -> list[dict[str, Any]]:
    return await _logins(request).team(p.tenant)  # type: ignore[no-any-return]


@router.post("/v1/admin/team", status_code=status.HTTP_201_CREATED, tags=["admin"])
async def add_member(body: MemberIn, request: Request, p: Admin) -> dict[str, Any]:
    try:
        return await _logins(request).add_member(  # type: ignore[no-any-return]
            p.tenant, p.id, body.name, body.email, body.roles
        )
    except ValueError as exc:
        code = status.HTTP_422_UNPROCESSABLE_CONTENT if "invalid" in str(exc) else 409
        raise HTTPException(code, str(exc)) from exc


@router.post(
    "/v1/admin/team/{principal_id}/invite",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["admin"],
)
async def invite(principal_id: MemberId, request: Request, p: Admin) -> None:
    try:
        await _logins(request).invite(p.tenant, principal_id, p.id)
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such team member") from exc
    except MailUnavailable as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "the invitation could not be sent by e-mail"
        ) from exc


@router.delete(
    "/v1/admin/team/{principal_id}", status_code=status.HTTP_204_NO_CONTENT, tags=["admin"]
)
async def remove_member(principal_id: MemberId, request: Request, p: Admin) -> None:
    try:
        done = await _logins(request).remove(p.tenant, principal_id, p.id)
    except ValueError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    if not done:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such team member")
