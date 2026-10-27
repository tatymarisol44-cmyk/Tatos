"""Login-based account connection; public callbacks require browser-bound state."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi import Path as ApiPath
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel, Field

from orchestrator.api.security import requires
from orchestrator.auth import Principal, Role
from orchestrator.meta_login import LoginError, Provider
from orchestrator.social_login import SocialLogin

router = APIRouter(tags=["social-login"])
Manager = Annotated[Principal, Depends(requires(Role.ADMIN, Role.OWNER, Role.MARKETING))]
FlowId = Annotated[str, ApiPath(pattern=r"^[a-f0-9]{32}$")]
COOKIE = "tatos_social_binding"
BRIDGE_FILE = Path(__file__).resolve().parent.parent / "web/social-connect.html"


class StartIn(BaseModel):
    professional_id: str | None = Field(default=None, pattern=r"^[\w.-]{1,64}$")


class ChoiceIn(BaseModel):
    choice: str = Field(pattern=r"^[a-f0-9]{32}$")


class LaunchIn(BaseModel):
    flow_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    state: str = Field(min_length=32, max_length=128)


class CompleteIn(LaunchIn):
    code: str | None = Field(default=None, max_length=8192)
    cancelled: bool = False


def service(request: Request) -> SocialLogin:
    return request.app.state.orchestrator.social_login  # type: ignore[no-any-return]


async def result(call: Any) -> Any:
    try:
        return await call
    except LoginError as exc:
        raise HTTPException(404 if str(exc) == "invalid_or_expired" else 409, str(exc)) from exc


def cookie(request: Request) -> str:
    value = request.cookies.get(COOKIE, "")
    return value if re.fullmatch(r"[0-9a-f]{64}", value) else ""


def same_origin(request: Request) -> None:
    if request.headers.get("origin") != service(request).settings.social_login_origin:
        raise HTTPException(403, "same origin required")


@router.get("/v1/social/login/configuration")
async def configuration(request: Request, _: Manager) -> dict[str, Any]:
    return service(request).configuration()


@router.post("/v1/social/login/{provider}/start")
async def start(provider: Provider, body: StartIn, request: Request, p: Manager) -> Any:
    from fastapi.responses import JSONResponse

    values, binding = await result(
        service(request).start(p, provider, cookie(request), body.professional_id)
    )
    response = JSONResponse(values)
    response.set_cookie(
        COOKIE,
        binding,
        httponly=True,
        secure=service(request).settings.app_env == "prod",
        samesite="lax",
        max_age=900,
        path="/v1/social/login",
    )
    return response


@router.get("/v1/social/login/{provider}/callback", include_in_schema=False)
async def callback(provider: Provider, request: Request) -> RedirectResponse:
    query = request.query_params
    if provider == "whatsapp" or any(len(query.getlist(k)) > 1 for k in ("state", "code", "error")):
        raise HTTPException(400, "invalid authorization response")
    state, code = query.get("state", ""), query.get("code")
    if not 32 <= len(state) <= 128 or (code is not None and len(code) > 8192):
        raise HTTPException(400, "invalid authorization response")
    await result(
        service(request).complete(
            provider, state, cookie(request), code, denied=bool(query.get("error"))
        )
    )
    # No provider code, state, token or caller-controlled return URL is reflected.
    return RedirectResponse(
        "/app#ajustes/redes", status_code=303, headers={"Cache-Control": "no-store"}
    )


@router.get("/v1/social/login/flows/{flow_id}")
async def status(flow_id: FlowId, request: Request, p: Manager) -> Any:
    return await result(service(request).status(p, flow_id))


@router.post("/v1/social/login/flows/{flow_id}/connect")
async def connect(flow_id: FlowId, body: ChoiceIn, request: Request, p: Manager) -> Any:
    return await result(service(request).connect(p, flow_id, body.choice))


@router.post("/v1/social/login/whatsapp/launch", include_in_schema=False)
async def launch(body: LaunchIn, request: Request) -> Any:
    same_origin(request)
    return await result(service(request).launch(body.flow_id, body.state, cookie(request)))


@router.post("/v1/social/login/whatsapp/complete", include_in_schema=False)
async def complete(body: CompleteIn, request: Request) -> dict[str, bool]:
    same_origin(request)
    await result(service(request).launch(body.flow_id, body.state, cookie(request)))
    await result(
        service(request).complete(
            "whatsapp", body.state, cookie(request), body.code, denied=body.cancelled
        )
    )
    return {"completed": True}


@router.get("/social-connect", include_in_schema=False)
async def bridge() -> FileResponse:
    return FileResponse(
        BRIDGE_FILE,
        headers={
            "Cache-Control": "no-store",
            "Referrer-Policy": "no-referrer",
            "Cross-Origin-Opener-Policy": "same-origin-allow-popups",
            "Content-Security-Policy": (
                "default-src 'none'; script-src 'self' https://connect.facebook.net; "
                "style-src 'self'; connect-src 'self' https://www.facebook.com "
                "https://graph.facebook.com; frame-src https://www.facebook.com; "
                "img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
            ),
        },
    )
