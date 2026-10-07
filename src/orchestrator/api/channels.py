"""Inbound channel events: the Telegram bot's webhook, so a STOP reply withdraws the
marketing consent (audit finding A11), and the WhatsApp webhook (ADR 0015 M4, below).

Telegram is told the URL `/v1/channels/telegram/{tenant}` and a secret with setWebhook;
it then sends the secret in `X-Telegram-Bot-Api-Secret-Token` on every call. Without
TELEGRAM_WEBHOOK_SECRET configured the endpoint does not exist (404). The reply is always
200 for authenticated calls, whatever the content, so Telegram does not retry and a
sender learns nothing about which chats are known."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from typing import Annotated, Any

from fastapi import APIRouter, Header, HTTPException, Query, Request, status
from fastapi import Path as FastAPIPath
from fastapi.responses import PlainTextResponse

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


# --- WhatsApp (Meta webhooks, ADR 0015 M4) ----------------------------------------------
# Read on 2026-10-07: Meta verifies the endpoint with GET hub.mode=subscribe, hub.verify_token
# and hub.challenge (answer with the challenge); every notification is signed with
# HMAC-SHA256 of the raw body and the App Secret in X-Hub-Signature-256 ("sha256=..."); the
# endpoint should answer 200, and failed deliveries are retried for up to 36 hours.


def _whatsapp_configured(orch: Orchestrator) -> tuple[str, str]:
    token, secret = orch.settings.whatsapp_verify_token, orch.settings.meta_app_secret
    if token is None or secret is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not Found")
    return token.get_secret_value(), secret.get_secret_value()


@router.get("/whatsapp", include_in_schema=False)
async def whatsapp_verify(
    request: Request,
    mode: Annotated[str | None, Query(alias="hub.mode")] = None,
    verify_token: Annotated[str | None, Query(alias="hub.verify_token")] = None,
    challenge: Annotated[str | None, Query(alias="hub.challenge", max_length=200)] = None,
) -> PlainTextResponse:
    expected, _ = _whatsapp_configured(request.app.state.orchestrator)
    if (
        mode != "subscribe"
        or verify_token is None
        or challenge is None
        or not hmac.compare_digest(verify_token.encode(), expected.encode())
    ):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "verification failed")
    return PlainTextResponse(challenge)


def signature_ok(secret: str, body: bytes, header: str | None) -> bool:
    if header is None or not header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(header.removeprefix("sha256="), expected)


def _text_messages(payload: Any) -> list[tuple[str, str, str, str]]:
    """(phone_number_id, message_id, sender, text) for each message; non-text messages
    carry an empty text, so they are recorded but never classified as anything else."""
    found: list[tuple[str, str, str, str]] = []
    if not isinstance(payload, dict) or payload.get("object") != "whatsapp_business_account":
        return found
    for entry in payload.get("entry") or []:
        for change in (entry or {}).get("changes") or []:
            if not isinstance(change, dict) or change.get("field") != "messages":
                continue
            value = change.get("value") or {}
            number_id = str((value.get("metadata") or {}).get("phone_number_id") or "")
            for message in value.get("messages") or []:
                if (
                    not isinstance(message, dict)
                    or not message.get("id")
                    or not message.get("from")
                ):
                    continue
                body = (
                    (message.get("text") or {}).get("body") if message.get("type") == "text" else ""
                )
                found.append(
                    (number_id, str(message["id"]), str(message["from"]), str(body or "")[:1000])
                )
    return found


@router.post("/whatsapp", include_in_schema=False)
async def whatsapp_webhook(
    request: Request,
    signature: Annotated[str | None, Header(alias="X-Hub-Signature-256")] = None,
) -> dict[str, Any]:
    orch: Orchestrator = request.app.state.orchestrator
    _, secret = _whatsapp_configured(orch)
    body = await request.body()
    if not signature_ok(secret, body, signature):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "bad signature")
    try:
        payload = json.loads(body)
    except ValueError:
        return {"ok": True}
    outcomes = [
        await orch.inbound.handle_message("whatsapp", number_id, message_id, sender, text)
        for number_id, message_id, sender, text in _text_messages(payload)
    ]
    if outcomes:
        log.info("whatsapp inbound: %s", sorted(set(outcomes)))
    return {"ok": True}
