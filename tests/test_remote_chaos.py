"""Chaos tests for the remote (A2A) path: whatever the remote agent does, the user gets
an answer in bounded time, flagged as a fallback."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx
import pytest

from orchestrator.catalog import Catalog
from orchestrator.config import Settings
from orchestrator.llm import FakeLLM
from orchestrator.remote import A2AClient
from orchestrator.service import Orchestrator

BASE = "http://jvm.test:8080"
REMOTE_ID = "remote-jvm-performance-specialist"
CARD = {
    "name": "JVM Performance Specialist",
    "description": "Diagnoses JVM memory and garbage collection problems.",
    "url": f"{BASE}/a2a",
}
DEADLINE = 0.3


async def _trickle() -> AsyncIterator[bytes]:
    while True:  # one byte at a time, each well inside any per-read timeout
        yield b" "
        await asyncio.sleep(0.02)


async def _drop_mid_body() -> AsyncIterator[bytes]:
    yield b'{"jsonrpc": "2.0", "id": "'
    raise httpx.ReadError("connection reset by peer")


def _ok(body: dict[str, Any], result: Any) -> httpx.Response:
    return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})


async def _hang(request: httpx.Request) -> httpx.Response:
    await asyncio.sleep(30)
    return httpx.Response(200)


RpcBehaviour = Callable[[httpx.Request], Any]

SCENARIOS: dict[str, RpcBehaviour] = {
    "hangs_before_headers": _hang,
    "trickles_forever": lambda r: httpx.Response(200, content=_trickle()),
    "drops_mid_body": lambda r: httpx.Response(200, content=_drop_mid_body()),
    "corrupt_bytes": lambda r: httpx.Response(200, content=b"\x00\xff<html>oops"),
    "http_503": lambda r: httpx.Response(503, text="overloaded"),
    "refuses_connection": lambda r: (_ for _ in ()).throw(httpx.ConnectError("refused")),
    "empty_answer": lambda r: _ok(
        json.loads(r.content), {"kind": "message", "parts": [{"kind": "text", "text": "  "}]}
    ),
    "wrong_shape": lambda r: _ok(json.loads(r.content), {"unexpected": True}),
    "json_rpc_error": lambda r: httpx.Response(
        200, json={"jsonrpc": "2.0", "id": 1, "error": {"code": -32603, "message": "boom"}}
    ),
}


async def _orchestrator(settings: Settings, catalog: Catalog, rpc: RpcBehaviour) -> Orchestrator:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=CARD)
        result = rpc(request)
        return await result if asyncio.iscoroutine(result) else result

    settings.remote_agents = [BASE]
    settings.remote_agent_timeout_s = DEADLINE
    client = A2AClient(settings, transport=httpx.MockTransport(handler))
    orch = Orchestrator(settings, catalog=catalog, llm=FakeLLM(), remote=client)
    await orch.start()
    return orch


@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
async def test_every_remote_failure_degrades_in_bounded_time(
    settings: Settings, catalog: Catalog, scenario: str
) -> None:
    orch = await _orchestrator(settings, catalog, SCENARIOS[scenario])
    started = time.perf_counter()
    result = await orch.chat("GC pauses", agent_id=REMOTE_ID, tenant="acme")
    elapsed = time.perf_counter() - started
    assert result.routing is not None
    assert result.routing["remote"] == {"status": "fallback", "error": "remote agent unavailable"}
    assert result.answer == "[# JVM Performance Specialist] GC pauses"  # LLM in the agent's role
    assert elapsed < DEADLINE + 1.0
    await orch.close()


@pytest.mark.parametrize("scenario", ["hangs_before_headers", "trickles_forever"])
async def test_slow_agent_does_not_stall_a_team(
    settings: Settings, catalog: Catalog, scenario: str
) -> None:
    orch = await _orchestrator(settings, catalog, SCENARIOS[scenario])
    started = time.perf_counter()
    result = await orch.chat(
        "tune GC and deploy",
        mode="team",
        agent_ids=[REMOTE_ID, "engineering-devops-automator"],
        tenant="acme",
    )
    assert time.perf_counter() - started < DEADLINE + 1.5
    assert result.team is not None
    by_agent = {r["agent_id"]: r for r in result.team["results"]}
    assert by_agent[REMOTE_ID]["remote"]["status"] == "fallback"
    assert by_agent["engineering-devops-automator"]["output"]
    await orch.close()


async def test_remote_id_colliding_with_a_local_agent_is_logged_as_error(
    settings: Settings, catalog: Catalog, caplog: pytest.LogCaptureFixture
) -> None:
    card = {**CARD, "name": "Frontend Developer"}  # -> remote-frontend-developer
    local = next(iter(catalog.agents.values()))
    catalog.agents["remote-frontend-developer"] = local  # simulate a clash

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=card)

    settings.remote_agents = [BASE]
    client = A2AClient(settings, transport=httpx.MockTransport(handler))
    orch = Orchestrator(settings, catalog=catalog, llm=FakeLLM(), remote=client)
    await orch.start()
    assert "NOT registered: id remote-frontend-developer is taken" in caplog.text
    assert any(r.levelname == "ERROR" for r in caplog.records)
    await orch.close()
