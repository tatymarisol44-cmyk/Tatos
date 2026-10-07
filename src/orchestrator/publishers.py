"""Platform adapters for publishing (ADR 0015, M3). Each follows the official flow read on
2026-10-07, and is called only after `social.check_publish` and a human approval.

Instagram (content-publishing guide): POST /<IG_ID>/media creates a container from an
`image_url` or a `video_url` with media_type=REELS; GET /<container>?fields=status_code
until FINISHED; POST /<IG_ID>/media_publish with creation_id.

TikTok (Content Posting API, direct post): POST /v2/post/publish/creator_info/query/, then
privacy_level must be one of its privacy_level_options (unaudited clients: SELF_ONLY only);
POST /v2/post/publish/video/init/ with source FILE_UPLOAD; PUT the file to upload_url with
Content-Range. Our videos are far below one chunk, so they go in a single PUT.

Tokens travel in the Authorization header, never in a URL (URLs end up in logs). Errors keep
the HTTP status and the platform's error code only, never the request."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import httpx

Sleep = Callable[[float], Awaitable[None]]


class PublishError(RuntimeError):
    """The platform refused or failed; the message carries no credentials."""


@dataclass(frozen=True)
class PublishResult:
    external_id: str
    visibility: Literal["public", "private"]
    mode: Literal["live", "dry_run"]
    detail: dict[str, Any] = field(default_factory=dict)


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _json(response: httpx.Response, step: str) -> dict[str, Any]:
    if response.status_code >= 400:
        code = ""
        try:
            body = response.json()
            error = body.get("error") if isinstance(body, dict) else None
            if isinstance(error, dict):
                code = str(error.get("code", ""))
        except ValueError:
            pass
        raise PublishError(f"{step}: HTTP {response.status_code} {code}".strip())
    try:
        data = response.json()
    except ValueError as exc:
        raise PublishError(f"{step}: response is not JSON") from exc
    if not isinstance(data, dict):
        raise PublishError(f"{step}: unexpected response")
    return data


class InstagramPublisher:
    def __init__(
        self,
        client: httpx.AsyncClient,
        base: str,
        *,
        poll_attempts: int = 10,
        poll_seconds: float = 3.0,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self.client = client
        self.base = base.rstrip("/")
        self.poll_attempts = poll_attempts
        self.poll_seconds = poll_seconds
        self.sleep = sleep

    async def publish(
        self,
        *,
        ig_user_id: str,
        token: str,
        media_type: Literal["image", "video"],
        media_url: str,
        caption: str,
    ) -> PublishResult:
        form = {"caption": caption}
        if media_type == "image":
            form["image_url"] = media_url
        else:
            form.update(video_url=media_url, media_type="REELS")
        created = _json(
            await self.client.post(
                f"{self.base}/{ig_user_id}/media", data=form, headers=_auth(token)
            ),
            "instagram container",
        )
        container = str(created.get("id", ""))
        if not container:
            raise PublishError("instagram container: no id")
        for attempt in range(self.poll_attempts):
            status = _json(
                await self.client.get(
                    f"{self.base}/{container}",
                    params={"fields": "status_code"},
                    headers=_auth(token),
                ),
                "instagram status",
            ).get("status_code")
            if status == "FINISHED":
                break
            if status in ("ERROR", "EXPIRED"):
                raise PublishError(f"instagram container {status}")
            if attempt < self.poll_attempts - 1:
                await self.sleep(self.poll_seconds)
        else:
            raise PublishError("instagram container not ready in time")
        published = _json(
            await self.client.post(
                f"{self.base}/{ig_user_id}/media_publish",
                data={"creation_id": container},
                headers=_auth(token),
            ),
            "instagram publish",
        )
        return PublishResult(
            str(published.get("id", "")), "public", "live", {"container": container}
        )


class WhatsAppSender:
    """Free-form service message inside the 24-hour window that opens when the person
    writes (read on 2026-10-07: POST /<PHONE_NUMBER_ID>/messages on graph.facebook.com)."""

    def __init__(self, client: httpx.AsyncClient, base: str) -> None:
        self.client = client
        self.base = base.rstrip("/")

    async def send_text(self, *, phone_number_id: str, token: str, to: str, body: str) -> str:
        response = await self.client.post(
            f"{self.base}/{phone_number_id}/messages",
            json={
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": to,
                "type": "text",
                "text": {"preview_url": False, "body": body},
            },
            headers=_auth(token),
        )
        messages = _json(response, "whatsapp send").get("messages") or []
        if not messages or not isinstance(messages[0], dict) or not messages[0].get("id"):
            raise PublishError("whatsapp send: no message id")
        return str(messages[0]["id"])


class TikTokPublisher:
    def __init__(self, client: httpx.AsyncClient, base: str) -> None:
        self.client = client
        self.base = base.rstrip("/")

    async def _call(self, path: str, token: str, body: dict[str, Any], step: str) -> dict[str, Any]:
        response = await self.client.post(
            f"{self.base}{path}",
            json=body,
            headers={**_auth(token), "Content-Type": "application/json; charset=UTF-8"},
        )
        payload = _json(response, step)
        error = payload.get("error") or {}
        if isinstance(error, dict) and error.get("code") not in (None, "ok"):
            raise PublishError(f"{step}: {error.get('code')}")
        data = payload.get("data")
        if not isinstance(data, dict):
            raise PublishError(f"{step}: no data")
        return data

    async def publish(
        self, *, token: str, video: Path, caption: str, audited: bool
    ) -> PublishResult:
        creator = await self._call(
            "/v2/post/publish/creator_info/query/", token, {}, "tiktok creator"
        )
        options = creator.get("privacy_level_options") or []
        privacy = "PUBLIC_TO_EVERYONE" if audited else "SELF_ONLY"
        if privacy not in options:
            raise PublishError(f"tiktok: privacy {privacy} not offered for this account")
        content = await asyncio.to_thread(video.read_bytes)
        size = len(content)
        init = await self._call(
            "/v2/post/publish/video/init/",
            token,
            {
                "post_info": {"title": caption, "privacy_level": privacy},
                "source_info": {
                    "source": "FILE_UPLOAD",
                    "video_size": size,
                    "chunk_size": size,
                    "total_chunk_count": 1,
                },
            },
            "tiktok init",
        )
        publish_id, upload_url = init.get("publish_id"), init.get("upload_url")
        if not publish_id or not upload_url:
            raise PublishError("tiktok init: no publish_id or upload_url")
        uploaded = await self.client.put(
            str(upload_url),
            content=content,
            headers={
                "Content-Type": "video/mp4",
                "Content-Length": str(size),
                "Content-Range": f"bytes 0-{size - 1}/{size}",
            },
        )
        if uploaded.status_code >= 400:
            raise PublishError(f"tiktok upload: HTTP {uploaded.status_code}")
        visibility: Literal["public", "private"] = (
            "public" if privacy == "PUBLIC_TO_EVERYONE" else "private"
        )
        return PublishResult(str(publish_id), visibility, "live")
