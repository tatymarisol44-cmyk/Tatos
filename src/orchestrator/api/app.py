"""FastAPI application: the SaaS entry point."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
from fastapi import Path as FastAPIPath
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from orchestrator import __version__
from orchestrator.api import (
    a2a,
    business,
    channels,
    establishment,
    patients,
    publications,
    social,
)
from orchestrator.api.body_limit import BodySizeLimit
from orchestrator.api.schemas import (
    AgentSummary,
    ChatRequest,
    ChatResponse,
    DocumentIn,
    DocumentOut,
    KnowledgeSearchRequest,
    RouteRequest,
)
from orchestrator.api.security import build_limiter, require_staff, require_tenant, requires
from orchestrator.api.views import staff_view, stream_event
from orchestrator.auth import Principal, Role
from orchestrator.config import Settings, get_settings
from orchestrator.governance import ThreadBusyError
from orchestrator.guardrails import check_input
from orchestrator.knowledge import KnowledgeRejected
from orchestrator.packs import pack_for
from orchestrator.service import Orchestrator, PendingReviewError, ThreadSubjectError
from orchestrator.surfaces import SurfaceDenied, ensure_allowed
from orchestrator.telemetry import setup_telemetry

log = logging.getLogger(__name__)
Staff = Annotated[Principal, Depends(require_staff)]
AdminDep = Annotated[Principal, Depends(requires(Role.ADMIN))]
PrivacyDep = Annotated[Principal, Depends(requires(Role.PRIVACY))]
WEB_DIR = Path(__file__).resolve().parent.parent / "web"
# The console is fully self-contained: no third-party origins, no inline script.
UI_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}


def _sse(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


async def outbox_worker(orch: Orchestrator, interval: int) -> None:
    """Campaign deliveries, retries after a crash and Telegram fallbacks, every few
    seconds. Safe on several replicas: each recipient is claimed with a conditional
    UPDATE before anything is sent."""
    while True:
        try:
            await orch.campaigns.run_outbox()
        except Exception:
            log.exception("outbox pass failed")
        await asyncio.sleep(interval)


def create_app(
    settings: Settings | None = None, orchestrator: Orchestrator | None = None
) -> FastAPI:
    settings = settings or get_settings()
    # Fail fast: a prod deployment without API keys would otherwise start and answer 503s
    # (resolve_tenant also refuses per request, as defence in depth).
    if settings.app_env == "prod" and not settings.tenant_keys():
        raise RuntimeError("APP_ENV=prod requires API_KEYS; refusing to start without auth")
    setup_telemetry(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        orch = orchestrator or Orchestrator(settings)
        app.state.orchestrator = orch
        worker: asyncio.Task[None] | None = None
        try:
            await orch.start()  # inside try: a failed start still releases pools
            if settings.outbox_interval_seconds > 0:
                worker = asyncio.create_task(outbox_worker(orch, settings.outbox_interval_seconds))
            yield
        finally:
            if worker is not None:
                worker.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await worker
            await orch.close()
            await app.state.limiter.close()

    app = FastAPI(
        title="Agency Orchestrator",
        version=__version__,
        summary="Routes each request to the right specialist agent, or orchestrates a team.",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.limiter = build_limiter(settings)
    app.add_middleware(
        BodySizeLimit,
        max_bytes=settings.max_body_bytes,
        overrides={"/v1/knowledge/documents": settings.max_document_body_bytes},
    )
    app.include_router(a2a.router, tags=["a2a"])
    app.include_router(business.router)
    app.include_router(patients.router)
    app.include_router(channels.router)
    app.include_router(social.router)
    app.include_router(publications.router)
    app.include_router(establishment.router)
    app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")

    if settings.otel_enabled:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        FastAPIInstrumentor.instrument_app(app, excluded_urls="healthz,readyz")

    def orch(request: Request) -> Orchestrator:
        return request.app.state.orchestrator  # type: ignore[no-any-return]

    @app.get("/", include_in_schema=False)
    async def console() -> FileResponse:
        return FileResponse(WEB_DIR / "index.html", headers=UI_HEADERS)

    @app.get("/healthz", tags=["ops"])
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz", tags=["ops"])
    async def readyz(request: Request) -> dict[str, object]:
        o = orch(request)
        if not o.ready:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "index not ready")
        return {
            "status": "ready",
            "agents": len(o.catalog),
            "catalog_version": o.catalog.version,
            "remote_agents": sorted(a.id for a in o.catalog.agents.values() if a.remote_url),
        }

    @app.get("/v1/agents", response_model=list[AgentSummary], tags=["agents"])
    async def list_agents(
        request: Request,
        division: str | None = Query(default=None),
        _tenant: str = Depends(require_tenant),
    ) -> list[AgentSummary]:
        return [
            AgentSummary(
                id=a.id,
                name=a.name,
                division=a.division,
                description=a.description,
                emoji=a.emoji,
                remote=bool(a.remote_url),
            )
            for a in sorted(orch(request).catalog.agents.values(), key=lambda a: a.id)
            if division is None or a.division == division
        ]

    @app.post("/v1/route", tags=["orchestration"])
    async def route(
        body: RouteRequest, request: Request, _tenant: str = Depends(require_tenant)
    ) -> dict[str, object]:
        guard = check_input(
            body.question,
            max_chars=settings.max_input_chars,
            injection_action=settings.injection_action,
            redact=settings.redact_pii,
        )
        if not guard.allowed:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, {"guardrails": guard.reasons})
        return (await orch(request).route(guard.text)).to_dict()

    @app.post("/v1/chat", response_model=ChatResponse, tags=["orchestration"])
    async def chat(
        body: ChatRequest,
        request: Request,
        principal: Staff,
    ) -> ChatResponse:
        """Answer a request (staff). `status` is `pending_review` when the answer was held
        for a human (see /v1/reviews); the thread then accepts no new message until
        resolved. Only reviewers see the held draft."""
        try:
            result = await orch(request).chat(
                body.question,
                thread_id=body.thread_id,
                agent_id=body.agent_id,
                agent_ids=body.agent_ids,
                mode=body.mode,
                tenant=principal.tenant,
                subject_id=body.subject_id,
                force_review=body.force_review,
                actor=principal.id,
            )
        except (PendingReviewError, ThreadSubjectError, ThreadBusyError) as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
        return ChatResponse(**staff_view(result.__dict__, principal))

    @app.post("/v1/chat/stream", tags=["orchestration"])
    async def chat_stream(
        body: ChatRequest,
        request: Request,
        principal: Staff,
    ) -> StreamingResponse:
        """Same as `/v1/chat`, as Server-Sent Events: `start`, `guardrails`, `evidence`,
        `routing` or `plan`, one `step` per specialist as it finishes (text only for
        reviewers), `review` if held for a human, then `done` (or `error`)."""
        try:
            events = await orch(request).chat_stream(
                body.question,
                thread_id=body.thread_id,
                agent_id=body.agent_id,
                agent_ids=body.agent_ids,
                mode=body.mode,
                tenant=principal.tenant,
                subject_id=body.subject_id,
                force_review=body.force_review,
                actor=principal.id,
            )
        except (PendingReviewError, ThreadSubjectError, ThreadBusyError) as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc

        async def body_iter() -> AsyncIterator[str]:
            try:
                async for event, data in events:
                    yield _sse(event, stream_event(event, data, principal))
            except Exception:
                log.exception("stream failed")
                yield _sse("error", {"message": "orchestration failed"})

        return StreamingResponse(
            body_iter(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # --- company knowledge base (RAG), always scoped to the caller's tenant -------
    @app.post(
        "/v1/knowledge/documents",
        response_model=DocumentOut,
        status_code=status.HTTP_201_CREATED,
        tags=["knowledge"],
    )
    async def add_document(body: DocumentIn, request: Request, admin: AdminDep) -> DocumentOut:
        tenant = admin.tenant
        if len(body.text) > settings.knowledge_max_doc_chars:
            raise HTTPException(
                status.HTTP_413_CONTENT_TOO_LARGE,
                f"document exceeds {settings.knowledge_max_doc_chars} characters",
            )
        try:  # a clinical kind shut out of retrieval is refused before anything is embedded
            ensure_allowed(pack_for(settings, tenant), body.kind, "rag")
        except SurfaceDenied as exc:
            raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
        try:
            info = await orch(request).knowledge.add(tenant, body.title, body.text, body.doc_id)
        except KnowledgeRejected as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
        return DocumentOut(**info.__dict__)

    @app.get("/v1/knowledge/documents", response_model=list[DocumentOut], tags=["knowledge"])
    async def list_documents(
        request: Request, tenant: str = Depends(require_tenant)
    ) -> list[DocumentOut]:
        docs = await orch(request).knowledge.documents(tenant)
        return [DocumentOut(**d.__dict__) for d in docs]

    @app.delete(
        "/v1/knowledge/documents/{doc_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        tags=["knowledge"],
    )
    async def delete_document(doc_id: str, request: Request, admin: AdminDep) -> None:
        # Another tenant's doc_id looks exactly like a missing one: no existence leak.
        if not await orch(request).knowledge.delete(admin.tenant, doc_id):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "document not found")

    @app.get("/v1/threads/{thread_id}", tags=["orchestration"])
    async def thread_status(
        request: Request,
        thread_id: Annotated[str, FastAPIPath(max_length=128, pattern=r"^[\w-]+$")],
        principal: Staff,
    ) -> dict[str, Any]:
        """Where a conversation stands: `pending_review` (no answer yet), `completed` with
        the approved answer, `rejected` or `blocked`. For polling after a held answer;
        the draft itself is only on /v1/reviews, for reviewers."""
        try:
            return await orch(request).thread_status(principal.tenant, thread_id)
        except KeyError as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "thread not found") from exc

    @app.delete("/v1/threads/{thread_id}", status_code=204, tags=["privacy"])
    async def delete_thread(
        request: Request,
        thread_id: Annotated[str, FastAPIPath(max_length=128, pattern=r"^[\w-]+$")],
        dpo: PrivacyDep,
    ) -> None:
        """Erase one conversation (its full checkpoint history) of the calling tenant."""
        if not await orch(request).delete_thread(dpo.tenant, thread_id):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "thread not found")

    @app.delete("/v1/threads", tags=["privacy"])
    async def delete_all_threads(request: Request, dpo: PrivacyDep) -> dict[str, int]:
        """Erase every conversation of the calling tenant (right to erasure, offboarding).
        Documents are erased separately via /v1/knowledge/documents."""
        return {"deleted": await orch(request).delete_tenant_threads(dpo.tenant)}

    @app.post("/v1/knowledge/search", tags=["knowledge"])
    async def search_knowledge(
        body: KnowledgeSearchRequest, request: Request, tenant: str = Depends(require_tenant)
    ) -> list[dict[str, Any]]:
        """Debug retrieval: which chunks a question would put in the agents' context."""
        chunks = await orch(request).knowledge.search(tenant, body.query, body.k)
        return [c.to_dict() for c in chunks]

    return app


def app_factory() -> FastAPI:
    """Entry point for `uvicorn --factory orchestrator.api.app:app_factory`."""
    return create_app()
