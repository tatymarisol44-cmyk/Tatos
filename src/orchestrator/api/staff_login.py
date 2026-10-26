"""Staff sign-in with e-mail, password and an authenticator app (ADR 0024), and the team as
the administration manages it.

- Public, no key: `/v1/auth/sign-in`, `code`, `link` (a personal link and a new password)
  and `enroll` (the authenticator's first code). Rate-limited per client address
  (AUTH_RATE_PER_MINUTE) on top of the per-login limits of staff_login.py; their answers
  never tell whether an e-mail exists.
- Signed in: `/v1/auth/sign-out` ends the session the request came with.
- Administration (role admin): `/v1/admin/team` lists and adds people, makes a new
  personal link and removes access."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from fastapi import Path as FastAPIPath
from pydantic import BaseModel, Field

from orchestrator.api.security import require_staff, requires
from orchestrator.auth import SESSION_PREFIX, Principal, Role
from orchestrator.staff_login import LinkError, SignInError, WeakPassword

router = APIRouter(tags=["auth"])
Admin = Annotated[Principal, Depends(requires(Role.ADMIN))]
Staff = Annotated[Principal, Depends(require_staff)]
MemberId = Annotated[str, FastAPIPath(pattern=r"^[\w.@:-]{1,128}$")]
WRONG = "wrong e-mail, password or code"
CODE = r"^\s*\d{6}\s*$"
CHALLENGE = r"^[0-9a-f]{32}$"


class SignInIn(BaseModel):
    email: str = Field(min_length=1, max_length=254)
    password: str = Field(min_length=1, max_length=200)


class CodeIn(BaseModel):
    challenge: str = Field(pattern=CHALLENGE)
    code: str = Field(pattern=CODE)
    remember: bool = False


class LinkIn(BaseModel):
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


@router.post("/v1/auth/sign-in", dependencies=[Throttled])
async def sign_in(body: SignInIn, request: Request) -> dict[str, str]:
    try:
        return await _logins(request).begin(body.email, body.password)  # type: ignore[no-any-return]
    except SignInError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, WRONG) from exc


@router.post("/v1/auth/code", dependencies=[Throttled])
async def code(body: CodeIn, request: Request) -> dict[str, str]:
    try:
        return await _logins(request).finish(body.challenge, body.code, body.remember)  # type: ignore[no-any-return]
    except SignInError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, WRONG) from exc


@router.post("/v1/auth/link", dependencies=[Throttled])
async def use_link(body: LinkIn, request: Request) -> dict[str, Any]:
    """A personal link and the new password: the answer is the authenticator to add."""
    try:
        return await _logins(request).use_link(body.token, body.password)  # type: ignore[no-any-return]
    except LinkError as exc:
        raise HTTPException(status.HTTP_410_GONE, "this link was used or expired") from exc
    except WeakPassword as exc:
        # Already in the person's language: the app shows it as it comes.
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc


@router.post("/v1/auth/enroll", dependencies=[Throttled])
async def enroll(body: CodeIn, request: Request) -> dict[str, str]:
    """The authenticator's first code: password and app saved, and signed in."""
    try:
        return await _logins(request).enroll(body.challenge, body.code, body.remember)  # type: ignore[no-any-return]
    except SignInError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, WRONG) from exc
    except LinkError as exc:
        raise HTTPException(status.HTTP_410_GONE, "this step was used or expired") from exc


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
    """The answer carries the person's link: send it to them, by WhatsApp for instance."""
    try:
        return await _logins(request).add_member(  # type: ignore[no-any-return]
            p.tenant, p.id, body.name, body.email, body.roles
        )
    except ValueError as exc:
        code = status.HTTP_422_UNPROCESSABLE_CONTENT if "invalid" in str(exc) else 409
        raise HTTPException(code, str(exc)) from exc


@router.post("/v1/admin/team/{principal_id}/link", tags=["admin"])
async def new_link(principal_id: MemberId, request: Request, p: Admin) -> dict[str, str]:
    """A new personal link: for an invitation not used, a forgotten password or a lost
    phone. The old password and app keep working until it is used."""
    try:
        return await _logins(request).link(p.tenant, principal_id, p.id)  # type: ignore[no-any-return]
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such team member") from exc


@router.delete(
    "/v1/admin/team/{principal_id}", status_code=status.HTTP_204_NO_CONTENT, tags=["admin"]
)
async def remove_member(
    principal_id: MemberId,
    request: Request,
    p: Admin,
    erase_profile: bool = Query(default=False),
) -> None:
    """Revoke access; explicit erase_profile also removes the e-mail login and pending
    credentials, allowing its e-mail to be invited again without reusing the old identity."""
    try:
        done = await _logins(request).remove(
            p.tenant, principal_id, p.id, erase_profile=erase_profile
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    if not done:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such team member")
