"""Contract test against a real A2A agent (the Java JVM specialist in CI).

Run with the agent up:  TEST_A2A_AGENT_URL=http://localhost:8080 pytest tests/test_remote_live.py
"""

from __future__ import annotations

import os

import pytest

from orchestrator.catalog import Catalog
from orchestrator.config import Settings
from orchestrator.llm import FakeLLM
from orchestrator.remote import A2AClient
from orchestrator.service import Orchestrator

AGENT_URL = os.environ.get("TEST_A2A_AGENT_URL")
REMOTE_ID = "remote-jvm-performance-specialist"

pytestmark = pytest.mark.skipif(AGENT_URL is None, reason="set TEST_A2A_AGENT_URL to run")


@pytest.fixture
async def orch(settings: Settings, catalog: Catalog) -> Orchestrator:
    settings.remote_agents = [AGENT_URL or ""]
    settings.router_use_llm = False
    o = Orchestrator(settings, catalog=catalog, llm=FakeLLM())
    await o.start()
    yield o  # type: ignore[misc]
    await o.close()


async def test_discovers_the_java_agent(orch: Orchestrator) -> None:
    spec = orch.catalog.agents[REMOTE_ID]
    assert spec.division == "remote"
    assert spec.remote_url == f"{(AGENT_URL or '').rstrip('/')}/a2a"
    assert "Garbage collection tuning" in spec.system_prompt


async def test_jvm_questions_are_routed_and_answered_by_java(orch: Orchestrator) -> None:
    result = await orch.chat(
        "Our Kubernetes pods get OOMKilled, how should we size the JVM heap?",
        thread_id="live",
        tenant="acme",
    )
    assert result.routing is not None
    assert result.routing["agent_id"] == REMOTE_ID
    assert result.routing["remote"] == {"status": "ok"}
    assert result.answer is not None and "MaxRAMPercentage" in result.answer
    assert result.usage["agent"]["model"] == f"a2a/{REMOTE_ID}"


async def test_team_mode_mixes_java_and_local_specialists(orch: Orchestrator) -> None:
    result = await orch.chat(
        "Tune JVM garbage collection and automate the deployment",
        mode="team",
        agent_ids=[REMOTE_ID, "engineering-devops-automator"],
        tenant="acme",
    )
    assert result.team is not None
    by_agent = {r["agent_id"]: r for r in result.team["results"]}
    assert by_agent[REMOTE_ID]["remote"] == {"status": "ok"}
    assert "Garbage collection" in by_agent[REMOTE_ID]["output"]


async def test_protocol_errors_surface_as_remote_errors(settings: Settings) -> None:
    from orchestrator.remote import RemoteAgentError

    client = A2AClient(settings)
    spec = await client.discover(AGENT_URL or "")
    with pytest.raises(RemoteAgentError, match="-32602"):
        await client.send(spec, "x" * 9000, "ctx")  # over the agent's input limit
    await client.close()
