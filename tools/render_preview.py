"""Authenticated, disposable Render preview; no production settings or data.

Run only with the dedicated preview Dockerfile and mandatory private-preview flag.
The original production image/start command and service are not changed.
"""

from __future__ import annotations

import base64
import binascii
import hmac
import os
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from starlette.responses import JSONResponse, RedirectResponse, Response
from tools.private_audit_lab import install_egress_guard, isolate_environment, make_app

USERNAME = "revisora"


def validated_origin(origin: str) -> str:
    parsed = urlsplit(origin)
    host = parsed.hostname or ""
    if (
        parsed.scheme != "https"
        or parsed.port not in (None, 443)
        or not host.startswith("tatos-pruebas-")
        or not host.endswith(".onrender.com")
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in ("", "/")
    ):
        raise ValueError("only a dedicated tatos-pruebas-* HTTPS origin is allowed")
    return origin.rstrip("/")


class ProtectedPreview:
    def __init__(self, app: Any, origin: str, password: str) -> None:
        self.origin = validated_origin(origin)
        if len(password) < 32:
            raise ValueError(
                "configure a private preview access password of at least 32 characters"
            )
        self.app, self.password = app, password.encode()

    def authorised(self, headers: dict[bytes, bytes]) -> bool:
        auth = headers.get(b"authorization", b"")
        if len(auth) > 4096 or not auth.startswith(b"Basic "):
            return False
        try:
            user, password = base64.b64decode(auth[6:], validate=True).split(b":", 1)
        except (ValueError, binascii.Error):
            return False
        return hmac.compare_digest(user, USERNAME.encode()) and hmac.compare_digest(
            password, self.password
        )

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers", []))
        # A shared invitation or a link from the review guide is a top-level HTML
        # navigation, not a cross-origin script/API request. Keep Basic Auth and
        # strict origin checks; allow only these document entry points to navigate.
        document_navigation = (
            scope["method"] in ("GET", "HEAD")
            and scope["path"] in {"/", "/lab", "/app", "/mi", "/mi/"}
            and headers.get(b"sec-fetch-mode") == b"navigate"
            and headers.get(b"sec-fetch-dest") == b"document"
        )
        response: Any = None
        if scope["path"] == "/preview-healthz" and scope["method"] in ("GET", "HEAD"):
            ready = bool(self.app.state.orchestrator.ready)
            response = JSONResponse(
                {"ready": ready, "environment": "private_preview", "real_operations": False},
                status_code=200 if ready else 503,
            )
        elif (
            headers.get(b"sec-fetch-site", b"none") not in (b"none", b"same-origin")
            and not document_navigation
        ) or headers.get(b"origin") not in (None, self.origin.encode()):
            response = Response("Same origin required", status_code=403)
        elif headers.get(b"host") != urlsplit(self.origin).netloc.encode():
            response = Response("Dedicated preview host required", status_code=403)
        elif not self.authorised(headers):
            response = Response(
                "Acceso privado a la prueba de Tatos. Usa las credenciales de prueba.",
                status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="Prueba privada Tatos", charset="UTF-8"'},
            )
        elif scope["path"] == "/":
            response = RedirectResponse("/lab", status_code=303)
        if response is not None:
            response.headers["Cache-Control"] = "no-store"
            response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["X-Frame-Options"] = "DENY"
            await response(scope, receive, send)
            return

        async def guarded_send(message: Any) -> None:
            if message["type"] == "http.response.start":
                message["headers"] = [
                    (k, v)
                    for k, v in message["headers"]
                    if k.lower() not in (b"cache-control", b"cross-origin-resource-policy")
                ] + [
                    (b"cache-control", b"no-store"),
                    (b"cross-origin-resource-policy", b"same-origin"),
                ]
            await send(message)

        await self.app(scope, receive, guarded_send)


def make_protected_app(media: Path, origin: str, password: str) -> ProtectedPreview:
    origin = validated_origin(origin)
    if len(password) < 32:
        raise ValueError("private preview access password is missing or too short")
    return ProtectedPreview(make_app(media, base_url=origin), origin, password)


def main() -> None:
    # Capture only dedicated preview inputs before discarding the inherited environment.
    if os.environ.get("TATOS_PRIVATE_PREVIEW") != "1" or os.environ.get("RENDER") != "true":
        raise RuntimeError("this entry point requires an explicitly configured Render preview")
    origin = validated_origin(os.environ.get("RENDER_EXTERNAL_URL", ""))
    password = os.environ.get("PREVIEW_ACCESS_PASSWORD", "")
    if len(password) < 32:
        raise RuntimeError("private preview access password is not configured")
    port = int(os.environ.get("PORT", "10000"))
    if not 1024 <= port <= 65535:
        raise ValueError("invalid preview port")
    isolate_environment()
    install_egress_guard()
    import uvicorn

    # Disposable only: no production connection strings, storage, tokens or accounts.
    with tempfile.TemporaryDirectory(prefix="tatos-render-preview-") as folder:
        app = make_protected_app(Path(folder), origin, password)
        # Render requires an external listener; every route is behind ProtectedPreview.
        uvicorn.run(
            app,
            host="0.0.0.0",  # noqa: S104 -- required Render binding behind mandatory authentication
            port=port,
            access_log=False,
            log_level="warning",
        )


if __name__ == "__main__":
    main()
