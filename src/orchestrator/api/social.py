"""Social channel accounts and platform rules (ADR 0015).

An admin connects the page, number or profile of the practice or of one professional. The
token never travels through this API: the account names a secret (`secret_ref`) that the
deployment fills, and the responses only say whether it is set."""

from __future__ import annotations

from dataclasses import asdict
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi import Path as FastAPIPath
from pydantic import BaseModel, Field

from orchestrator.api.security import require_staff, requires
from orchestrator.auth import Principal, Role
from orchestrator.establishment import ProfessionalError
from orchestrator.inbound import ReplyRefused
from orchestrator.service import Orchestrator
from orchestrator.social import (
    NETWORKS,
    PLATFORM_RULES,
    REF_PATTERN,
    AccountConflict,
    Network,
    network_verified,
)

router = APIRouter(prefix="/v1/social", tags=["social"])
Admin = Annotated[Principal, Depends(requires(Role.ADMIN))]
Staff = Annotated[Principal, Depends(require_staff)]


class AccountIn(BaseModel):
    network: Network
    external_id: str = Field(min_length=1, max_length=128, pattern=r"^[\w.:@+-]+$")
    handle: str = Field(min_length=1, max_length=120)
    secret_ref: str = Field(
        pattern=REF_PATTERN,
        description="Name of the secret that holds the token (set as SOCIAL_SECRET_<name>). "
        "Never the token itself.",
    )
    professional_id: str | None = Field(default=None, max_length=64, pattern=r"^[\w.-]+$")
    audited: bool = Field(
        default=False, description="TikTok only: the API client passed TikTok's audit."
    )


def orch(request: Request) -> Orchestrator:
    return request.app.state.orchestrator  # type: ignore[no-any-return]


@router.get("/rules")
async def platform_rules(_: Staff) -> dict[str, Any]:
    """What each platform allows, with the page it comes from and whether it was read."""
    return {
        "networks": {n: {"verified": network_verified(n)} for n in NETWORKS},
        "rules": [asdict(r) for r in PLATFORM_RULES],
    }


@router.post("/accounts", status_code=status.HTTP_201_CREATED)
async def connect_account(body: AccountIn, request: Request, p: Admin) -> dict[str, Any]:
    try:
        return await orch(request).social.add(
            p.tenant,
            p.id,
            network=body.network,
            external_id=body.external_id,
            handle=body.handle,
            secret_ref=body.secret_ref,
            professional_id=body.professional_id,
            audited=body.audited,
        )
    except AccountConflict as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except ProfessionalError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc


@router.get("/accounts")
async def list_accounts(
    request: Request,
    p: Staff,
    professional_id: Annotated[str | None, Query(max_length=64, pattern=r"^[\w.-]+$")] = None,
) -> list[dict[str, Any]]:
    return await orch(request).social.list(p.tenant, professional_id)


Care = Annotated[Principal, Depends(requires(Role.RECEPTION, Role.REVIEWER, Role.OWNER))]


@router.get("/alerts")
async def list_alerts(
    request: Request,
    p: Care,
    state: Annotated[Literal["open", "resolved", "all"], Query()] = "open",
) -> list[dict[str, Any]]:
    """Crisis and "talk to a person" alerts from incoming messages. The caller's number is
    shown so someone can call back; every read is audited."""
    return await orch(request).inbound.alerts(p.tenant, p.id, None if state == "all" else state)


class ReplyIn(BaseModel):
    text: str = Field(min_length=1, max_length=4096)


@router.post("/alerts/{alert_id}/reply")
async def reply_to_alert(
    body: ReplyIn,
    request: Request,
    alert_id: Annotated[str, FastAPIPath(max_length=32, pattern=r"^[0-9a-f]+$")],
    p: Care,
) -> dict[str, Any]:
    """A person writes back by WhatsApp, inside the 24-hour window and only if the number
    did not opt out. The text is sent, not stored."""
    try:
        return await orch(request).inbound.reply(p.tenant, alert_id, p.id, body.text)
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "alert not found") from exc
    except ReplyRefused as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc


@router.post("/alerts/{alert_id}/resolve", status_code=status.HTTP_204_NO_CONTENT)
async def resolve_alert(
    request: Request,
    alert_id: Annotated[str, FastAPIPath(max_length=32, pattern=r"^[0-9a-f]+$")],
    p: Care,
) -> None:
    if not await orch(request).inbound.resolve(p.tenant, alert_id, p.id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "open alert not found")


@router.delete("/accounts/{account_id}", status_code=status.HTTP_204_NO_CONTENT)
async def disable_account(
    request: Request,
    account_id: Annotated[str, FastAPIPath(max_length=32, pattern=r"^[0-9a-f]+$")],
    p: Admin,
) -> None:
    # Another tenant's account looks exactly like a missing one.
    if not await orch(request).social.disable(p.tenant, account_id, p.id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "active account not found")
