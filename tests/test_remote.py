from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from orchestrator.api.app import create_app
from orchestrator.catalog import Catalog
from orchestrator.config import Settings
from orchestrator.llm import FakeLLM
from orchestrator.remote import (
    A2AClient,
    RemoteAgentError,
    card_to_spec,
    context_id,
    discover_all,
)
from orchestrator.service import Orchestrator

BASE = "http://jvm.test:8080"
CARD = {
    "protocolVersion": "0.3.0",
    "name": "JVM Performance Specialist",
    "description": "Diagnoses JVM memory, garbage collection and heap sizing problems.",
    "url": f"{BASE}/a2a",
    "preferredTransport": "JSONRPC",
    "skills": [
        {
            "id": "gc",
            "name": "Garbage collection tuning",
            "description": "G1, ZGC, pause times",
            "tags": ["jvm", "gc"],
        },
        {"id": "heap", "name": "Heap sizing", "description": "Xmx in containers"},
        "not-a-dict",
        {"id": "nameless", "description": "skipped: no name"},
    ],
}


@dataclass
class FakeRemoteAgent:
    """In-process stand-in for the Java agent, speaking A2A over httpx.MockTransport."""

    card: dict[str, Any] = field(default_factory=lambda: dict(CARD))
    reply: Callable[[dict[str, Any]], Any] | None = None
    down: bool = False
    requests: list[httpx.Request] = field(default_factory=list)

    def rpc_bodies(self) -> list[dict[str, Any]]:
        return [json.loads(r.content) for r in self.requests if r.method == "POST"]

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.down:
            raise httpx.ConnectError("connection refused", request=request)
        if request.method == "GET" and request.url.path == "/.well-known/agent-card.json":
            return httpx.Response(200, json=self.card)
        if request.method == "POST" and request.url.path == "/a2a":
            body = json.loads(request.content)
            if self.reply is not None:
                return httpx.Response(200, json=self.reply(body))
            message = body["params"]["message"]
            text = message["parts"][0]["text"]
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "result": {
                        "kind": "message",
                        "role": "agent",
                        "messageId": "m1",
                        "contextId": message["contextId"],
                        "parts": [{"kind": "text", "text": f"JVM says: {text}"}],
                    },
                },
            )
        return httpx.Response(404)

    def client(self, settings: Settings) -> A2AClient:
        return A2AClient(settings, transport=httpx.MockTransport(self.handler))


def _rpc(body: dict[str, Any], result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": body["id"], "result": result}


@pytest.fixture
def remote_settings(settings: Settings) -> Settings:
    settings.remote_agents = [BASE]
    settings.remote_discovery_attempts = 2
    settings.remote_discovery_backoff_s = 0.0
    return settings


@pytest.fixture
def agent() -> FakeRemoteAgent:
    return FakeRemoteAgent()


@pytest.fixture
async def orch(remote_settings: Settings, catalog: Catalog, agent: FakeRemoteAgent) -> Orchestrator:
    o = Orchestrator(
        remote_settings, catalog=catalog, llm=FakeLLM(), remote=agent.client(remote_settings)
    )
    await o.start()
    return o


REMOTE_ID = "remote-jvm-performance-specialist"


# --- agent card -> catalog entry -------------------------------------------------


def test_card_becomes_a_routable_spec() -> None:
    spec = card_to_spec(BASE, CARD)
    assert spec.id == REMOTE_ID
    assert spec.division == "remote"
    assert spec.remote_url == f"{BASE}/a2a"
    assert "## Core Mission\nDiagnoses JVM memory" in spec.system_prompt
    assert "- Garbage collection tuning: G1, ZGC, pause times (tags: jvm, gc)" in spec.system_prompt
    assert "- Heap sizing: Xmx in containers\n" in spec.system_prompt
    assert "Garbage collection tuning" in spec.index_text()
    assert "skipped" not in spec.system_prompt


def test_card_url_defaults_and_resolves_relative_paths() -> None:
    assert card_to_spec(BASE, {**CARD, "url": None}).remote_url == f"{BASE}/a2a"
    assert card_to_spec(BASE + "/", {**CARD, "url": "/rpc"}).remote_url == f"{BASE}/rpc"
    # Same origin with an explicit default port is still the same origin.
    https = card_to_spec("https://a.test", {**CARD, "url": "https://a.test:443/a2a"})
    assert https.remote_url == "https://a.test:443/a2a"


@pytest.mark.parametrize(
    ("card", "error"),
    [
        ({**CARD, "name": "  "}, "name and a description"),
        ({**CARD, "description": 42}, "name and a description"),
        ({**CARD, "preferredTransport": "GRPC"}, "unsupported transport"),
        ({**CARD, "url": "http://evil.test/a2a"}, "not on the agent's origin"),
        ({**CARD, "url": "http://jvm.test:9999/a2a"}, "not on the agent's origin"),
        ({**CARD, "url": "https://jvm.test:8080/a2a"}, "not on the agent's origin"),
        ({**CARD, "name": "!!!"}, "no usable characters"),
    ],
)
def test_invalid_cards_are_rejected(card: dict[str, Any], error: str) -> None:
    with pytest.raises(ValueError, match=error):
        card_to_spec(BASE, card)


def test_context_id_is_stable_and_opaque() -> None:
    first = context_id("acme:t1", REMOTE_ID)
    assert first == context_id("acme:t1", REMOTE_ID)
    assert first != context_id("globex:t1", REMOTE_ID)
    assert first != context_id("acme:t1", "remote-other")
    assert "acme" not in first


# --- discovery -----------------------------------------------------------------


async def test_discover_all_skips_bad_agents(remote_settings: Settings) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if host == "down.test":
            raise httpx.ConnectTimeout("timeout", request=request)
        if host == "error.test":
            return httpx.Response(500)
        if host == "html.test":
            return httpx.Response(200, text="<html>")
        if host == "list.test":
            return httpx.Response(200, json=[CARD])
        if host == "huge.test":
            return httpx.Response(200, content=b"x" * 1_000_001)
        if host == "bad.test":
            return httpx.Response(200, json={**CARD, "url": "http://elsewhere.test/a2a"})
        if host == "redirect.test":
            return httpx.Response(302, headers={"Location": f"{BASE}/.well-known/agent-card.json"})
        return httpx.Response(200, json={**CARD, "url": f"http://{host}/a2a"})

    client = A2AClient(remote_settings, transport=httpx.MockTransport(handler))
    urls = [
        "http://down.test",
        "http://error.test",
        "http://html.test",
        "http://list.test",
        "http://huge.test",
        "http://bad.test",
        "http://redirect.test",
        "http://ok.test",
        "http://dup.test",  # same card name as ok.test -> duplicate id
        "http://localhost:8000",  # this orchestrator itself (public_base_url)
    ]
    specs = await discover_all(client, urls)
    await client.close()
    assert [(s.id, s.remote_url) for s in specs] == [(REMOTE_ID, "http://ok.test/a2a")]


async def test_discovery_retries_agents_that_start_late(remote_settings: Settings) -> None:
    state = {"calls": 0, "fail_first": 1}

    def handler(request: httpx.Request) -> httpx.Response:
        state["calls"] += 1
        if state["calls"] <= state["fail_first"]:  # still booting
            raise httpx.ConnectError("refused", request=request)
        return httpx.Response(200, json=CARD)

    client = A2AClient(remote_settings, transport=httpx.MockTransport(handler))
    [spec] = await discover_all(client, [BASE])
    assert spec.id == REMOTE_ID and state["calls"] == 2

    # Never up within the attempts: tried exactly remote_discovery_attempts times, skipped.
    state.update(calls=0, fail_first=99)
    assert await discover_all(client, [BASE]) == []
    assert state["calls"] == remote_settings.remote_discovery_attempts
    await client.close()


async def test_catalog_version_and_index_include_remote_agents(
    remote_settings: Settings, catalog: Catalog, agent: FakeRemoteAgent
) -> None:
    before = catalog.version
    orch = Orchestrator(
        remote_settings, catalog=catalog, llm=FakeLLM(), remote=agent.client(remote_settings)
    )
    await orch.start()
    assert catalog.version != before
    assert "remote" in catalog.divisions
    assert catalog.agents[REMOTE_ID].remote_url == f"{BASE}/a2a"
    remote_settings.router_use_llm = False
    decision = await orch.route("jvm garbage collection pauses and heap sizing")
    assert decision.agent_id == REMOTE_ID
    # Re-adding the same agent is a no-op and keeps the version stable.
    version = catalog.version
    assert catalog.add_remote([catalog.agents[REMOTE_ID]]) == []
    assert catalog.version == version
    await orch.close()


# --- message/send ----------------------------------------------------------------


async def test_send_parses_message_and_sends_api_key(remote_settings: Settings) -> None:
    remote_settings.remote_agents_api_key = SecretStr("s3cret")
    agent = FakeRemoteAgent()
    client = agent.client(remote_settings)
    spec = card_to_spec(BASE, CARD)
    assert await client.send(spec, "why OOM?", "ctx-1") == "JVM says: why OOM?"
    rpc = agent.requests[-1]
    assert rpc.headers["X-API-Key"] == "s3cret"
    body = json.loads(rpc.content)
    assert body["method"] == "message/send"
    assert body["params"]["message"]["contextId"] == "ctx-1"
    assert body["params"]["message"]["role"] == "user"


@pytest.mark.parametrize(
    ("result", "text"),
    [
        (
            {"kind": "task", "artifacts": [{"parts": [{"kind": "text", "text": "a1"}]}, "x"]},
            "a1",
        ),
        (
            {
                "kind": "task",
                "artifacts": [],
                "status": {"message": {"parts": [{"kind": "text", "text": "from status"}]}},
            },
            "from status",
        ),
        (
            {"kind": "message", "parts": [{"kind": "text", "text": "a"}, {"kind": "data"}, "x"]},
            "a",
        ),
    ],
)
async def test_send_reads_task_results(
    remote_settings: Settings, result: dict[str, Any], text: str
) -> None:
    agent = FakeRemoteAgent(reply=lambda body: _rpc(body, result))
    assert await agent.client(remote_settings).send(card_to_spec(BASE, CARD), "q", "c") == text


@pytest.mark.parametrize(
    ("reply", "error"),
    [
        (
            lambda b: {"jsonrpc": "2.0", "id": b["id"], "error": {"code": -32601, "message": "no"}},
            "JSON-RPC error -32601: no",
        ),
        (lambda b: {"jsonrpc": "2.0", "id": b["id"], "error": "boom"}, "JSON-RPC error None"),
        (lambda b: _rpc({"id": "other"}, {"kind": "message", "parts": []}), "id does not match"),
        (lambda b: _rpc(b, {"kind": "message", "parts": []}), "no text parts"),
        (lambda b: _rpc(b, "just a string"), "no text parts"),
        (lambda b: [1, 2], "not an object"),
    ],
)
async def test_send_rejects_bad_replies(
    remote_settings: Settings, reply: Callable[[dict[str, Any]], Any], error: str
) -> None:
    agent = FakeRemoteAgent(reply=reply)
    with pytest.raises(RemoteAgentError, match=error):
        await agent.client(remote_settings).send(card_to_spec(BASE, CARD), "q", "c")


async def test_send_caps_reply_length(remote_settings: Settings) -> None:
    remote_settings.remote_agent_max_chars = 10
    long = {"kind": "message", "parts": [{"kind": "text", "text": "y" * 500}]}
    agent = FakeRemoteAgent(reply=lambda b: _rpc(b, long))
    assert await agent.client(remote_settings).send(card_to_spec(BASE, CARD), "q", "c") == "y" * 10


async def test_discover_rejects_non_object_card(remote_settings: Settings) -> None:
    agent = FakeRemoteAgent(card=["nope"])  # type: ignore[arg-type]
    with pytest.raises(RemoteAgentError, match="not an object"):
        await agent.client(remote_settings).discover(BASE)


# --- end to end through the graph -------------------------------------------------


async def test_single_mode_delegates_to_remote_agent(
    orch: Orchestrator, agent: FakeRemoteAgent
) -> None:
    await orch.knowledge.add("acme", "Heap policy", "Production JVMs use ZGC and 8 GB heaps.")
    first = await orch.chat(
        "heap policy for ZGC?", agent_id=REMOTE_ID, thread_id="t", tenant="acme"
    )
    assert first.answer == "JVM says: heap policy for ZGC?"
    assert first.routing is not None and first.routing["remote"] == {"status": "ok"}
    assert first.usage["agent"]["model"] == f"a2a/{REMOTE_ID}"
    # Tenant documents stay inside our boundary by default.
    [body] = agent.rpc_bodies()
    assert "ZGC and 8 GB" not in body["params"]["message"]["parts"][0]["text"]

    await orch.chat("and G1?", agent_id=REMOTE_ID, thread_id="t", tenant="acme")
    await orch.chat("and G1?", agent_id=REMOTE_ID, thread_id="t", tenant="globex")
    contexts = [b["params"]["message"]["contextId"] for b in agent.rpc_bodies()]
    assert contexts[0] == contexts[1] != contexts[2]


async def test_knowledge_is_shared_only_when_allowed(
    orch: Orchestrator, agent: FakeRemoteAgent
) -> None:
    orch.settings.remote_share_knowledge = True
    await orch.knowledge.add("acme", "Heap policy", "Production JVMs use ZGC and 8 GB heaps.")
    await orch.chat("heap policy for ZGC?", agent_id=REMOTE_ID, tenant="acme")
    [body] = agent.rpc_bodies()
    assert "ZGC and 8 GB" in body["params"]["message"]["parts"][0]["text"]


async def test_remote_outage_falls_back_to_local_llm(
    orch: Orchestrator, agent: FakeRemoteAgent
) -> None:
    agent.down = True
    result = await orch.chat("why OOM?", agent_id=REMOTE_ID, tenant="acme")
    assert result.answer == "[# JVM Performance Specialist] why OOM?"
    assert result.routing is not None
    assert result.routing["remote"] == {"status": "fallback", "error": "remote agent unavailable"}


async def test_output_guard_applies_to_remote_replies(
    orch: Orchestrator, agent: FakeRemoteAgent
) -> None:
    leak = {"kind": "message", "parts": [{"kind": "text", "text": "mail ops@acme.io now"}]}
    agent.reply = lambda body: _rpc(body, leak)
    result = await orch.chat("who to contact?", agent_id=REMOTE_ID, tenant="acme")
    assert result.answer is not None and "ops@acme.io" not in result.answer


async def test_team_mode_mixes_remote_and_local_agents(
    orch: Orchestrator, agent: FakeRemoteAgent
) -> None:
    result = await orch.chat(
        "tune the JVM and deploy it",
        mode="team",
        agent_ids=[REMOTE_ID, "engineering-devops-automator"],
        tenant="acme",
    )
    assert result.team is not None
    by_agent = {r["agent_id"]: r for r in result.team["results"]}
    assert by_agent[REMOTE_ID]["remote"] == {"status": "ok"}
    assert by_agent[REMOTE_ID]["output"].startswith("JVM says:")
    assert "remote" not in by_agent["engineering-devops-automator"]
    assert len(agent.rpc_bodies()) == 1


async def test_team_worker_falls_back_when_remote_is_down(
    orch: Orchestrator, agent: FakeRemoteAgent
) -> None:
    agent.down = True
    result = await orch.chat("tune the JVM", mode="team", agent_ids=[REMOTE_ID], tenant="acme")
    assert result.team is not None
    [step] = result.team["results"]
    assert step["remote"]["status"] == "fallback" and step["error"] is None


@pytest.fixture
def api(
    remote_settings: Settings, catalog: Catalog, agent: FakeRemoteAgent
) -> Iterator[TestClient]:
    orch = Orchestrator(
        remote_settings, catalog=catalog, llm=FakeLLM(), remote=agent.client(remote_settings)
    )
    with TestClient(create_app(remote_settings, orch)) as c:
        yield c


def test_readiness_reports_remote_agents(api: TestClient) -> None:
    assert api.get("/readyz").json()["remote_agents"] == [REMOTE_ID]


def test_api_lists_remote_agents(api: TestClient) -> None:
    agents = api.get("/v1/agents", headers={"X-API-Key": "test-key"}).json()
    remote = [a for a in agents if a["remote"]]
    assert [a["id"] for a in remote] == [REMOTE_ID]
    assert all(not a["remote"] for a in agents if a["id"] != REMOTE_ID)


def test_orchestrator_builds_its_own_client_from_settings(
    remote_settings: Settings, catalog: Catalog
) -> None:
    assert isinstance(
        Orchestrator(remote_settings, catalog=catalog, llm=FakeLLM()).remote, A2AClient
    )
    remote_settings.remote_agents = []
    assert Orchestrator(remote_settings, catalog=catalog, llm=FakeLLM()).remote is None
