"""The inbox: WhatsApp, Instagram Direct and Facebook Messenger in one place (ADR 0023).
What patients write may be clinical, so every route is for the clinician role (need to
know: admin and service keys get 403) and every read is audited."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi import Path as FastAPIPath
from pydantic import BaseModel, Field

from orchestrator.api.security import require_clinician
from orchestrator.auth import Principal
from orchestrator.inbox import MAX_BODY, InboxError, InboxService

router = APIRouter(prefix="/v1/inbox", tags=["inbox"])

Clinician = Annotated[Principal, Depends(require_clinician)]
ConversationId = Annotated[str, FastAPIPath(pattern=r"^[0-9a-f]{1,32}$")]
MessageId = Annotated[str, FastAPIPath(pattern=r"^[0-9a-f]{1,32}$")]


class ReplyIn(BaseModel):
    body: str = Field(min_length=1, max_length=MAX_BODY)
    draft_id: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{1,32}$",
        description="The AI draft this reply comes from (approved as is or edited)",
    )


class LinkIn(BaseModel):
    patient_id: str = Field(pattern=r"^[\w.-]{1,64}$")


def service(request: Request) -> InboxService:
    return request.app.state.orchestrator.inbox  # type: ignore[no-any-return]


def _not_found() -> HTTPException:
    return HTTPException(status.HTTP_404_NOT_FOUND, "conversation not found")


@router.get("")
async def list_conversations(
    request: Request,
    p: Clinician,
    network: Annotated[Literal["whatsapp", "instagram", "facebook"] | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[dict[str, Any]]:
    return await service(request).conversations(p.tenant, p.id, network=network, limit=limit)


@router.get("/{conversation_id}")
async def open_conversation(
    conversation_id: ConversationId, request: Request, p: Clinician
) -> dict[str, Any]:
    try:
        return await service(request).conversation(p.tenant, conversation_id, p.id)
    except KeyError as exc:
        raise _not_found() from exc


@router.post("/{conversation_id}/draft", status_code=status.HTTP_201_CREATED)
async def draft_reply(
    conversation_id: ConversationId, request: Request, p: Clinician
) -> dict[str, Any]:
    """A reply suggested by the AI, kept as a draft: nothing is sent."""
    try:
        return await service(request).draft(p.tenant, conversation_id, p.id)
    except KeyError as exc:
        raise _not_found() from exc
    except InboxError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc


@router.post("/{conversation_id}/reply", status_code=status.HTTP_201_CREATED)
async def send_reply(
    body: ReplyIn, conversation_id: ConversationId, request: Request, p: Clinician
) -> dict[str, Any]:
    try:
        return await service(request).reply(
            p.tenant, conversation_id, p.id, body.body, draft_id=body.draft_id
        )
    except KeyError as exc:
        raise _not_found() from exc
    except InboxError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc


@router.delete("/{conversation_id}/drafts/{draft_id}", status_code=status.HTTP_204_NO_CONTENT)
async def discard_draft(
    conversation_id: ConversationId, draft_id: MessageId, request: Request, p: Clinician
) -> None:
    if not await service(request).discard(p.tenant, conversation_id, draft_id, p.id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "draft not found")


@router.put("/{conversation_id}/patient")
async def link_patient(
    body: LinkIn, conversation_id: ConversationId, request: Request, p: Clinician
) -> dict[str, Any]:
    """Tie the conversation to a patient's record, so it shows in their file."""
    try:
        return await service(request).link_patient(p.tenant, conversation_id, body.patient_id, p.id)
    except KeyError as exc:
        raise _not_found() from exc
    except InboxError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc


@router.post("/{conversation_id}/close", status_code=status.HTTP_204_NO_CONTENT)
async def close_conversation(
    conversation_id: ConversationId, request: Request, p: Clinician
) -> None:
    try:
        await service(request).close(p.tenant, conversation_id, p.id)
    except KeyError as exc:
        raise _not_found() from exc


@router.delete("/{conversation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_conversation(
    conversation_id: ConversationId, request: Request, p: Clinician
) -> None:
    try:
        await service(request).delete_conversation(p.tenant, conversation_id, p.id)
    except KeyError as exc:
        raise _not_found() from exc
    except InboxError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
