"""Request metrics for the SLOs (docs/slo.md): duration by route TEMPLATE, method and
status class. The template (`/v1/crm/patients/{patient_id}`), never the raw path, so a
label cannot carry a patient id and the series count stays bounded."""

from __future__ import annotations

import time

from orchestrator.api.body_limit import ASGIApp, Message, Receive, Scope, Send
from orchestrator.telemetry import HTTP_DURATION


class RequestMetrics:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        start = time.perf_counter()
        status = 500  # an exception before the response starts is a server error

        async def capture(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = int(message["status"])
            await send(message)

        try:
            await self.app(scope, receive, capture)
        finally:
            route = scope.get("route")
            HTTP_DURATION.record(
                time.perf_counter() - start,
                {
                    "route": getattr(route, "path", None) or "unmatched",
                    "method": scope.get("method", ""),
                    "status_class": f"{status // 100}xx",
                },
            )
