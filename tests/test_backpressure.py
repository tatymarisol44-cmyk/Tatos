"""Backpressure on model routes (api/backpressure.py): a pod at capacity refuses the next
model request at once with 503 + Retry-After, holds the slot for a whole streamed answer,
and never limits the agenda, CRM or records."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

from orchestrator.api.backpressure import ModelConcurrencyLimit


def toy(release: asyncio.Event, entered: asyncio.Semaphore) -> Starlette:
    async def slow(request: Request) -> JSONResponse:
        entered.release()
        await release.wait()
        return JSONResponse({"ok": True})

    async def stream(request: Request) -> StreamingResponse:
        entered.release()

        async def body() -> AsyncIterator[bytes]:
            yield b"event: start\n\n"
            await release.wait()
            yield b"event: done\n\n"

        return StreamingResponse(body(), media_type="text/event-stream")

    async def crm(request: Request) -> JSONResponse:
        return JSONResponse({"patients": []})

    return Starlette(
        routes=[
            Route("/v1/chat", slow, methods=["POST"]),
            Route("/v1/chat/stream", stream, methods=["POST"]),
            Route("/v1/crm/patients", crm, methods=["GET", "POST"]),
            Route("/v1/route", slow, methods=["GET", "POST"]),
        ]
    )


async def test_a_full_pod_refuses_at_once_and_frees_slots_when_done() -> None:
    release, entered = asyncio.Event(), asyncio.Semaphore(0)
    limiter = ModelConcurrencyLimit(toy(release, entered), max_inflight=2, retry_after_s=3)
    first: list[dict[str, Any]] = []
    second: list[dict[str, Any]] = []
    running = [
        asyncio.create_task(_call(limiter, "/v1/chat", first)),
        asyncio.create_task(_call(limiter, "/v1/chat", second)),
    ]
    await entered.acquire()
    await entered.acquire()
    assert limiter.inflight == 2

    refused: list[dict[str, Any]] = []
    await _call(limiter, "/v1/chat", refused)
    start = refused[0]
    assert start["status"] == 503
    assert (b"retry-after", b"3") in start["headers"]
    body = json.loads(refused[1]["body"])
    assert body["code"] == "busy" and "Nothing was lost" in body["detail"]
    # The rest of the product is not limited by a chat spike.
    crm: list[dict[str, Any]] = []
    await _call(limiter, "/v1/crm/patients", crm)
    assert crm[0]["status"] == 200

    release.set()
    await asyncio.wait_for(asyncio.gather(*running), 5)
    assert first[0]["status"] == second[0]["status"] == 200
    assert limiter.inflight == 0
    again: list[dict[str, Any]] = []
    await _call(limiter, "/v1/chat", again)
    assert again[0]["status"] == 200


def test_only_posts_to_model_routes_are_limited() -> None:
    limited = ModelConcurrencyLimit.limited
    assert limited({"method": "POST", "path": "/v1/chat"})
    assert limited({"method": "POST", "path": "/v1/chat/stream"})
    assert limited({"method": "POST", "path": "/v1/me/chat"})
    assert limited({"method": "POST", "path": "/a2a"})
    assert not limited({"method": "GET", "path": "/v1/route"})
    assert not limited({"method": "GET", "path": "/v1/me/chat/t1"})
    assert not limited({"method": "POST", "path": "/v1/chatbot"})  # prefix, not substring
    assert not limited({"method": "POST", "path": "/v1/crm/patients"})


async def _call(
    app: Any, path: str, sent: list[dict[str, Any]], first_body: asyncio.Event | None = None
) -> None:
    """Drive the ASGI app directly: httpx's in-process transport buffers a whole
    response, so it cannot observe a stream that has started but not finished."""
    scope = {"type": "http", "method": "POST", "path": path, "headers": [], "query_string": b""}
    done = False

    async def receive() -> dict[str, Any]:
        nonlocal done
        if not done:
            done = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await asyncio.Event().wait()  # never disconnects
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)
        if first_body is not None and message.get("body"):
            first_body.set()

    await app(scope, receive, send)


async def test_a_streamed_answer_holds_its_slot_until_the_last_event() -> None:
    release, entered = asyncio.Event(), asyncio.Semaphore(0)
    limiter = ModelConcurrencyLimit(toy(release, entered), max_inflight=1)
    streamed: list[dict[str, Any]] = []
    started = asyncio.Event()
    reader = asyncio.create_task(_call(limiter, "/v1/chat/stream", streamed, started))
    await entered.acquire()
    await asyncio.wait_for(started.wait(), 5)  # the first event is out: it has started...
    refused: list[dict[str, Any]] = []
    await _call(limiter, "/v1/chat", refused)
    assert refused[0]["status"] == 503  # ...and the slot is still held
    release.set()
    await asyncio.wait_for(reader, 5)
    assert b"event: done" in b"".join(m.get("body", b"") for m in streamed)
    assert limiter.inflight == 0


async def test_zero_disables_the_limit() -> None:
    async def app(scope: Any, receive: Any, send: Any) -> None:
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    limiter = ModelConcurrencyLimit(app, max_inflight=0)
    transport = httpx.ASGITransport(app=limiter)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        assert (await client.post("/v1/chat")).status_code == 204
