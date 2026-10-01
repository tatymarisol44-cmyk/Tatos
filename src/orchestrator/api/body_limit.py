"""Request body size limit, enforced while the body arrives (audit finding A27).

Field limits (`max_length` on `question`) only run after the whole body has been read and
parsed, so without this a client could send megabytes in an ignored field. This ASGI
middleware rejects with 413:

- at once, when `Content-Length` already says the body is too big;
- as soon as the bytes received exceed the limit, for chunked uploads with no length.

Routes that legitimately take more (document upload) get their own, larger limit. A
reverse proxy (nginx `client_max_body_size`, an ingress annotation) should still set the
same cap in front, so oversized bodies never reach Python at all."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

from starlette.exceptions import HTTPException

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

TOO_LARGE = b'{"detail":"request body too large"}'


class BodySizeLimit:
    def __init__(self, app: ASGIApp, max_bytes: int, overrides: dict[str, int]) -> None:
        self.app = app
        self.max_bytes = max_bytes
        self.overrides = overrides  # exact path -> limit

    def limit_for(self, path: str) -> int:
        return self.overrides.get(path, self.max_bytes)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        limit = self.limit_for(scope.get("path", ""))
        for name, value in scope.get("headers", []):
            if name == b"content-length":
                try:
                    declared = int(value)
                except ValueError:
                    declared = -1
                if declared > limit or declared < 0:
                    await _reject(send, 413 if declared > limit else 400)
                    return
        received = 0

        async def limited() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    # FastAPI re-raises HTTPException from body reading as-is.
                    raise HTTPException(413, "request body too large")
            return message

        await self.app(scope, limited, send)


async def _reject(send: Send, status: int) -> None:
    body = TOO_LARGE if status == 413 else b'{"detail":"invalid Content-Length"}'
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
                (b"connection", b"close"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})
