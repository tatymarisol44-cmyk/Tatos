"""Inbound channel events. For now: the Telegram bot's webhook, so a STOP reply withdraws
the marketing consent (audit finding A11).

Telegram is told the URL `/v1/channels/telegram/{tenant}` and a secret with setWebhook;
it then sends the secret in `X-Telegram-Bot-Api-Secret-Token` on every call. Without
TELEGRAM_WEBHOOK_SECRET configured the endpoint does not exist (404). The reply is always
200 for authenticated calls, whatever the content, so Telegram does not retry and a
sender learns nothing about which chats are known."""

from __future__ import annotations

import hmac
import logging
from typing import Annotated, Any

from fastapi import APIRouter, Header, HTTPException, Request, status
from fastapi import Path as FastAPIPath

from orchestrator.service import Orchestrator

log = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/channels", tags=["channels"])

TENANT = r"^[\w.-]{1,64}$"


@router.post("/telegram/{tenant}", include_in_schema=False)
async def telegram_webhook(
    request: Request,
    tenant: Annotated[str, FastAPIPath(pattern=TENANT)],
    secret: Annotated[str | None, Header(alias="X-Telegram-Bot-Api-Secret-Token")] = None,
) -> dict[str, Any]:
    orch: Orchestrator = request.app.state.orchestrator
    expected = orch.settings.telegram_webhook_secret
    if expected is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not Found")
    if secret is None or not hmac.compare_digest(
        secret.encode(), expected.get_secret_value().encode()
    ):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "bad webhook secret")
    if tenant not in set(orch.settings.tenant_keys().values()):
        return {"ok": True}
    try:
        update = await request.json()
    except ValueError:
        return {"ok": True}
    message = update.get("message") if isinstance(update, dict) else None
    if not isinstance(message, dict):
        return {"ok": True}
    chat = message.get("chat") or {}
    text = message.get("text")
    if not isinstance(text, str) or "id" not in chat:
        return {"ok": True}
    outcome = await orch.campaigns.handle_inbound(tenant, str(chat["id"]), text[:200])
    log.info("telegram inbound for %s: %s", tenant, outcome)
    return {"ok": True}
