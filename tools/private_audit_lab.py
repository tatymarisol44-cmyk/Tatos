"""Disposable loopback lab over the existing staff/patient app, never production.

Not started by importing this module. Requires an explicit reviewed launch. No keys
are printed or written. All identities and money are synthetic; state is in memory.
"""

from __future__ import annotations

import ipaddress
import json
import os
import secrets
import socket
import sys
import tempfile
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import Request

ROOT = Path(__file__).resolve().parents[1]
PORT = 8795
BASE = f"http://127.0.0.1:{PORT}"


def isolate_environment() -> None:
    allowed = {
        "path",
        "systemroot",
        "windir",
        "temp",
        "tmp",
        "userprofile",
        "localappdata",
        "appdata",
        "programfiles",
        "programfiles(x86)",
        "comspec",
        "pathext",
    }
    for name in list(os.environ):
        if name.casefold() not in allowed:
            os.environ.pop(name, None)


def install_egress_guard() -> None:
    """Process-local barrier below SDKs. Other programs/processes are unaffected."""
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    original_resolve = socket.getaddrinfo

    def local(host: Any) -> bool:
        if host == "localhost":
            return True
        try:
            return ipaddress.ip_address(host).is_loopback
        except ValueError:
            return False

    def connect(sock: Any, address: Any) -> Any:
        if not isinstance(address, tuple) or not local(address[0]):
            raise OSError("Private lab: external network disabled")
        return original_connect(sock, address)

    def connect_ex(sock: Any, address: Any) -> int:
        if not isinstance(address, tuple) or not local(address[0]):
            raise OSError("Private lab: external network disabled")
        return original_connect_ex(sock, address)

    def resolve(host: Any, *args: Any, **kwargs: Any) -> Any:
        if not local(host):
            raise OSError("Private lab: external DNS disabled")
        return original_resolve(host, *args, **kwargs)

    socket.socket.connect = connect
    socket.socket.connect_ex = connect_ex
    socket.getaddrinfo = resolve


def make_app(media: Path, *, base_url: str = BASE) -> Any:
    from fastapi import HTTPException
    from fastapi.responses import HTMLResponse, Response
    from pydantic import SecretStr

    from orchestrator.api.app import UI_HEADERS, create_app
    from orchestrator.auth import Role
    from orchestrator.catalog import load_catalog
    from orchestrator.config import Settings
    from orchestrator.db import utcnow
    from orchestrator.llm import FakeLLM
    from orchestrator.service import Orchestrator

    parsed_origin = urlsplit(base_url)
    if parsed_origin.username or parsed_origin.password or parsed_origin.path not in ("", "/"):
        raise ValueError("a plain origin is required")
    if parsed_origin.hostname not in ("127.0.0.1", "localhost") and parsed_origin.scheme != "https":
        raise ValueError("remote previews require HTTPS and the protected Render entry point")
    allowed_hosts = {parsed_origin.netloc}
    allowed_origins = {base_url.rstrip("/")}
    if base_url == BASE:
        allowed_hosts.add(f"localhost:{PORT}")
        allowed_origins.add(f"http://localhost:{PORT}")

    # Local-only disposable service identities. Never import real env files or vaults.
    roots = {name: secrets.token_urlsafe(32) for name in ("lab-a", "lab-b", "lab-platform")}
    identities: dict[str, tuple[str, bool]] = {}
    settings = Settings(
        _env_file=None,
        app_env="test",
        agents_dir=ROOT / "vendor/agency-agents",
        llm_backend="fake",
        embedding_backend="hashing",
        vector_backend="memory",
        checkpointer_backend="memory",
        database_url=SecretStr("sqlite+aiosqlite:///:memory:"),
        api_keys=SecretStr(",".join(f"{key}:{name}" for name, key in roots.items())),
        tenant_packs={name: "ec-psychologist" for name in roots},
        media_dir=media,
        outbox_interval_seconds=0,
        publish_poll_seconds=0,
        billing_enabled=True,
        billing_monthly_price_cents=10000,
        billing_platform_tenant="lab-platform",
        billing_dlocal_live_enabled=False,
        billing_automation_enabled=False,
        clinical_vision_enabled=False,
        public_base_url=base_url,
        practice_display_name="Consultorio ficticio — laboratorio",
        rate_limit_per_minute=5000,
    )
    orch = Orchestrator(settings, catalog=load_catalog(settings.agents_dir), llm=FakeLLM())
    app = create_app(settings, orch)
    original_lifespan = app.router.lifespan_context

    async def seed() -> None:
        import httpx

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=base_url
        ) as client:

            async def call(method: str, path: str, body: Any = None, key: str | None = None) -> Any:
                result = await client.request(
                    method, path, json=body, headers={"X-API-Key": key or roots["lab-a"]}
                )
                if result.status_code >= 400:
                    raise RuntimeError(
                        f"Synthetic seed failed: {method} {path} HTTP {result.status_code}"
                    )
                return result.json() if result.status_code != 204 else None

            _, clinician = await orch.principals.create_staff(
                "lab-a", "dra-demo", list(Role), "lab"
            )
            _, owner = await orch.principals.create_staff(
                "lab-a", "duena-demo", [Role.OWNER, Role.MARKETING], "lab"
            )
            _, reception = await orch.principals.create_staff(
                "lab-a", "recepcion-demo", [Role.RECEPTION], "lab"
            )
            identities.update(
                {
                    "clinician": (clinician, False),
                    "owner": (owner, False),
                    "reception": (reception, False),
                    "tenant-b": (roots["lab-b"], False),
                    "platform": (roots["lab-platform"], False),
                }
            )
            await call(
                "POST",
                "/v1/admin/professionals",
                {
                    "professional_id": "dra-demo",
                    "display_name": "Doctora de prueba",
                    "pack_id": "ec-psychologist",
                    "staff_id": "dra-demo",
                },
            )
            await call(
                "PUT",
                "/v1/agenda/professionals/dra-demo/hours",
                {
                    "hours": [
                        {"day": d, "hours": "09:00-13:00", "slot_minutes": 60}
                        for d in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
                    ]
                },
            )
            await call(
                "PUT",
                "/v1/billing/bank",
                {"instructions": "DATOS FICTICIOS: no transferir ni cargar información real."},
            )
            await call(
                "PUT",
                "/v1/billing/platform/bank",
                {"instructions": "PLATAFORMA FICTICIA: no realizar pagos."},
                roots["lab-platform"],
            )
            survey = await call(
                "POST",
                "/v1/surveys/definitions",
                {
                    "spec": {
                        "title": "Experiencia de atención (prueba)",
                        "questions": [
                            {
                                "id": "atencion",
                                "text": "¿Cómo fue la atención?",
                                "minimum": 1,
                                "maximum": 5,
                            }
                        ],
                        "critical_below": 40,
                    }
                },
            )
            await call(
                "PUT",
                "/v1/surveys/configuration",
                {
                    "revision": 0,
                    "settings": {
                        "enabled": True,
                        "survey_id": survey["id"],
                        "version": 1,
                        "after_minutes": 0,
                        "channels": ["app"],
                    },
                },
            )
            for n, label in enumerate(("Pendiente", "Abono", "Pagado", "Encuesta")):
                pid = f"paciente-demo-{n}"
                await call(
                    "POST",
                    "/v1/crm/patients",
                    {
                        "id": pid,
                        "display_name": f"Paciente ficticia — {label}",
                        "phone": f"+1555000000{n}",
                    },
                )
                for purpose in ("surveys", "marketing", "whatsapp_billing"):
                    await call(
                        "PUT",
                        f"/v1/subjects/{pid}/consents/{purpose}",
                        {"granted": True, "source": "synthetic-private-lab"},
                    )
                visit = await call(
                    "POST",
                    "/v1/crm/appointments",
                    {
                        "patient_id": pid,
                        "starts_at": (utcnow() + timedelta(days=n + 1))
                        .replace(hour=15, minute=0, second=0, microsecond=0)
                        .isoformat(),
                        "duration_min": 60,
                        "professional_id": "dra-demo",
                        "price": 50,
                    },
                )
                order = await call("POST", f"/v1/billing/appointments/{visit['id']}")
                await call(
                    "PUT",
                    f"/v1/billing/orders/{order['invoice_id']}/deadline",
                    {"revision": 1, "due_at": (utcnow() - timedelta(days=1)).isoformat()},
                )
                if n in (1, 2):
                    await call(
                        "POST",
                        f"/v1/billing/orders/{order['invoice_id']}/credits",
                        {
                            "amount_cents": 2000 if n == 1 else 5000,
                            "confirmed": True,
                            "bank_transaction_id": f"FICTICIO-{n}",
                            "request_id": f"lab-credit-{n}",
                            "note": "Ingreso inventado, no verificado con banco real",
                        },
                    )
                if n == 3:
                    _, patient = await orch.principals.create_patient_access("lab-a", pid, "lab", 1)
                    identities["patient"] = (patient, True)
                    await call(
                        "POST",
                        f"/v1/crm/appointments/{visit['id']}/status",
                        {"status": "completed"},
                    )
            await call("POST", "/v1/billing/subscription")
            await call(
                "PUT",
                "/v1/marketing/brand",
                {
                    "version": 0,
                    "brand": {
                        "name": "Consultorio ficticio",
                        "services": ["Orientación profesional de prueba"],
                        "background": "#FFFFFF",
                        "accent": "#006666",
                        "text": "#222222",
                    },
                },
            )
            account = await call(
                "POST",
                "/v1/social/accounts",
                {
                    "network": "instagram",
                    "external_id": "lab-instagram",
                    "handle": "@ficticio",
                    "secret_ref": "LAB_UNCONFIGURED",
                },
            )
            plan = await call(
                "POST",
                "/v1/marketing/workflows",
                {
                    "brief": {
                        "objective": "Presentar los servicios públicos ficticios",
                        "audience": "Público general",
                        "network": "instagram",
                    },
                    "first": utcnow().date().isoformat(),
                },
            )
            await call(
                "POST",
                f"/v1/marketing/workflows/{plan['id']}/render",
                {
                    "revision": plan["revision"],
                    "account_id": account["account_id"],
                    "formats": ["feed", "story", "square"],
                },
            )
            await call(
                "PUT",
                "/v1/tax-trials/issuers",
                {
                    "version": 0,
                    "profile": {
                        "ruc": "0000000000001",
                        "legal_name": "Emisor ficticio",
                        "trade_name": "Consultorio de ensayo",
                        "address": "Dirección inventada",
                        "establishment": "001",
                        "emission_point": "001",
                        "tax_regime": "Ficticio, no verificado",
                    },
                },
            )

    @asynccontextmanager
    async def lifespan(application: Any) -> Any:
        async with original_lifespan(application):
            await seed()
            yield

    app.router.lifespan_context = lifespan

    @app.middleware("http")
    async def private_boundary(request: Request, call_next: Any) -> Any:
        if request.headers.get("host") not in allowed_hosts:
            return Response("Preview host required", status_code=403)
        origin = request.headers.get("origin")
        if origin and origin not in allowed_origins:
            return Response("Same origin required", status_code=403)
        # Exercise the existing invitation/password/MFA flow only against this
        # disposable in-memory staff store. No real accounts or mail transport.
        preview_auth_paths = {
            "/v1/auth/link",
            "/v1/auth/enroll",
            "/v1/auth/sign-in",
            "/v1/auth/code",
            "/v1/auth/sign-out",
        }
        if request.url.path.startswith("/v1/session/email") or (
            request.url.path.startswith("/v1/auth/") and request.url.path not in preview_auth_paths
        ):
            return Response(
                "Real account registration is disabled in this disposable lab", status_code=403
            )
        result = await call_next(request)
        if request.url.path in {"/app", "/mi/", "/mi"} and "text/html" in result.headers.get(
            "content-type", ""
        ):
            content = b"".join([chunk async for chunk in result.body_iterator])
            content = content.replace(
                b"</head>", b'<script src="/lab/banner.js" defer></script></head>'
            )
            headers = dict(result.headers)
            headers.pop("content-length", None)
            return Response(content, status_code=result.status_code, headers=headers)
        return result

    @app.get("/lab", include_in_schema=False)
    async def guide() -> Any:
        rows = "".join(
            f'<li><a href="/lab/enter/{who}">{label}</a></li>'
            for who, label in (
                ("clinician", "Doctora: agenda, Tests, cobros, encuestas y configuración"),
                ("owner", "Dueña: marketing, diseños y reportes"),
                ("patient", "Paciente: reservar y contestar encuesta"),
                ("reception", "Recepción: sin historias clínicas"),
                ("tenant-b", "Otro consultorio: datos separados"),
                ("platform", "Administración Tatos: mensualidades ficticias"),
            )
        )
        return HTMLResponse(
            '<!doctype html><html lang="es"><meta charset="utf-8">'
            "<title>Laboratorio privado Tatos</title><h1>Datos ficticios, entorno privado</h1>"
            "<p>Sin red externa, dinero ni mensajes reales. IA FakeLLM simulada; "
            "diseños y datos locales. No es un sandbox verificado de proveedor "
            f"ni una validación clínica.</p><ul>{rows}</ul>"
            "<p>Ajustes → Fuentes de Tests / Prototipo fiscal. "
            "Marketing → Planes y diseños / Encuestas. Cobros → Ver pago → Saldo "
            "y avisos de ensayo. No cargues datos reales. "
            "Las invitaciones permiten ensayar contraseña y autenticador con cuentas "
            "temporales: usa datos ficticios y una contraseña exclusiva para esta prueba. "
            "Al cerrar el proceso se pierde el estado.</p></html>",
            headers={**UI_HEADERS, "Cache-Control": "no-store"},
        )

    @app.get("/lab/enter/{who}", include_in_schema=False)
    async def enter(who: str) -> Any:
        if who not in identities:
            raise HTTPException(404)
        return HTMLResponse(
            '<!doctype html><html lang="es"><meta charset="utf-8">'
            "<title>Entrando al laboratorio</title>"
            f'<script src="/lab/entry/{who}.js" defer></script>'
            "<p>Abriendo pantalla privada con identidad ficticia…</p></html>",
            headers={**UI_HEADERS, "Cache-Control": "no-store"},
        )

    @app.get("/lab/entry/{who}.js", include_in_schema=False)
    async def entry(who: str) -> Any:
        if who not in identities:
            raise HTTPException(404)
        key, patient = identities[who]
        script = (
            f'localStorage.setItem("mi.key",{json.dumps(key)});location.replace("/mi/#encuestas");'
            if patient
            else f'sessionStorage.setItem("consultorio.key",{json.dumps(key)});'
            'location.replace("/app#hoy");'
        )
        return Response(
            script, media_type="application/javascript", headers={"Cache-Control": "no-store"}
        )

    @app.get("/lab/banner.js", include_in_schema=False)
    async def banner() -> Any:
        return Response(
            'document.addEventListener("DOMContentLoaded",()=>{'
            'const bar=document.createElement("aside");bar.className="card";'
            'bar.textContent="LABORATORIO PRIVADO · DATOS FICTICIOS · IA SIMULADA · '
            'SIN COBROS, SRI O MENSAJES REALES. ";const a=document.createElement("a");'
            'a.href="/lab";a.textContent="Cambiar perfil de prueba";bar.append(a);'
            'document.body.prepend(bar);document.addEventListener("click",e=>{'
            'const a=e.target.closest("a");'
            "if(a&&new URL(a.href,location.href).origin!==location.origin){"
            "e.preventDefault();e.stopImmediatePropagation();"
            'alert("Enlaces externos desactivados en el laboratorio");}},true);});',
            media_type="application/javascript",
        )

    return app


if __name__ == "__main__":
    isolate_environment()
    install_egress_guard()
    sys.path.insert(0, str(ROOT / "src"))
    import uvicorn

    with tempfile.TemporaryDirectory(prefix="private-lab-", dir=ROOT) as folder:
        uvicorn.run(make_app(Path(folder)), host="127.0.0.1", port=PORT, access_log=False)
