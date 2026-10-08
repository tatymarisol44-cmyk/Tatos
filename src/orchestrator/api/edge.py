"""Headers every response carries behind the HTTPS gateway (ADR 0016).

* `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer` and
  `X-Frame-Options: DENY` on everything.
* `Cache-Control: no-store` on the API (`/v1/...`): its answers can hold personal and
  health data, so no browser or intermediate proxy may keep a copy. A route that sets its
  own Cache-Control (the SSE stream) keeps it.
* `Strict-Transport-Security` when configured: browsers that saw it once refuse plain
  HTTP to this host afterwards. TLS itself ends at the gateway.

Pure ASGI, so streaming responses pass through untouched."""

from __future__ import annotations

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send


class SecurityHeaders:
    def __init__(self, app: ASGIApp, hsts_max_age_seconds: int = 0) -> None:
        self.app = app
        self.hsts = (
            f"max-age={hsts_max_age_seconds}; includeSubDomains" if hsts_max_age_seconds else None
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        api = scope["path"].startswith("/v1/")

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers.setdefault("X-Content-Type-Options", "nosniff")
                headers.setdefault("Referrer-Policy", "no-referrer")
                headers.setdefault("X-Frame-Options", "DENY")
                if api:
                    headers.setdefault("Cache-Control", "no-store")
                if self.hsts:
                    headers.setdefault("Strict-Transport-Security", self.hsts)
            await send(message)

        await self.app(scope, receive, send_with_headers)
