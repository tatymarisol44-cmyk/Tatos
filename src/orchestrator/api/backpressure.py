"""Backpressure for the routes that call a model (audit 2026-10-08, scalability).

A model call waits seconds on the provider. Without a bound, a traffic spike piles up
in-flight conversations in one pod: memory grows, database connections run out, the
provider's rate limit is hit and every request slows down together. Instead, each pod
serves at most `MAX_INFLIGHT_MODEL_REQUESTS` of them at once and answers the next one
with 503 + Retry-After immediately. Clients retry, the load balancer spreads load, and
the HPA adds pods (the `ApiBusy` alert says when it cannot keep up).

The slot is held for the whole response, a streamed answer included. The other routes
(agenda, CRM, records) are never limited by this: a chat spike cannot take them down."""

from __future__ import annotations

import json

from orchestrator.api.body_limit import ASGIApp, Receive, Scope, Send
from orchestrator.telemetry import BUSY_REJECTED, MODEL_INFLIGHT

MODEL_ROUTES = (
    "/v1/chat",  # also /v1/chat/stream
    "/v1/me/chat",
    "/v1/insights/ask",
    "/v1/route",
    "/a2a",
)
BUSY = json.dumps(
    {
        "detail": "The assistant is busy right now. Nothing was lost; try again in a moment.",
        "code": "busy",
    }
).encode()


class ModelConcurrencyLimit:
    def __init__(self, app: ASGIApp, max_inflight: int, retry_after_s: int = 2) -> None:
        self.app = app
        self.max_inflight = max_inflight
        self.retry_after = str(retry_after_s)
        self.inflight = 0  # one event loop per process: a plain counter is enough

    @staticmethod
    def limited(scope: Scope) -> bool:
        path = scope.get("path", "")
        return scope.get("method") == "POST" and any(
            path == r or path.startswith(r + "/") for r in MODEL_ROUTES
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or self.max_inflight <= 0 or not self.limited(scope):
            await self.app(scope, receive, send)
            return
        if self.inflight >= self.max_inflight:
            BUSY_REJECTED.add(1)
            await send(
                {
                    "type": "http.response.start",
                    "status": 503,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"retry-after", self.retry_after.encode()),
                        (b"content-length", str(len(BUSY)).encode()),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": BUSY})
            return
        self.inflight += 1
        MODEL_INFLIGHT.add(1)
        try:
            await self.app(scope, receive, send)
        finally:
            self.inflight -= 1
            MODEL_INFLIGHT.add(-1)
