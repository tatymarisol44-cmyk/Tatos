"""MCP server (stdio): exposes the orchestrator as tools for Claude Desktop, Claude Code,
Cursor or any MCP client."""

from __future__ import annotations

import asyncio
from typing import Any

from mcp.server.mcpserver import MCPServer

from orchestrator.config import get_settings
from orchestrator.service import Orchestrator

mcp = MCPServer("agency-orchestrator")
_orch: Orchestrator | None = None
_lock = asyncio.Lock()


async def _get() -> Orchestrator:
    global _orch
    async with _lock:
        if _orch is None:
            _orch = Orchestrator(get_settings())
            await _orch.start()
    return _orch


@mcp.tool()
async def list_agents(division: str | None = None) -> list[dict[str, str]]:
    """List specialist agents, optionally filtered by division (e.g. 'engineering')."""
    orch = await _get()
    return [
        {"id": a.id, "name": a.name, "division": a.division, "description": a.description}
        for a in sorted(orch.catalog.agents.values(), key=lambda a: a.id)
        if division is None or a.division == division
    ]


@mcp.tool()
async def route_question(question: str) -> dict[str, Any]:
    """Return which specialist agent should handle a question, with candidates and confidence."""
    return (await (await _get()).route(question)).to_dict()


@mcp.tool()
async def ask(
    question: str, agent_id: str | None = None, thread_id: str | None = None
) -> dict[str, Any]:
    """Answer a question with the best specialist (or `agent_id` if given)."""
    result = await (await _get()).chat(
        question, agent_id=agent_id, thread_id=thread_id, tenant="mcp"
    )
    return result.__dict__


@mcp.tool()
async def ask_team(
    question: str, agent_ids: list[str] | None = None, thread_id: str | None = None
) -> dict[str, Any]:
    """Orchestrate a team: a planner splits the request across several specialists (or
    exactly `agent_ids`), they work in parallel and a synthesizer merges their answers."""
    result = await (await _get()).chat(
        question, mode="team", agent_ids=agent_ids, thread_id=thread_id, tenant="mcp"
    )
    return result.__dict__


@mcp.tool()
async def search_knowledge(query: str, k: int = 4) -> list[dict[str, Any]]:
    """Search the company knowledge base (documents uploaded for the `mcp` tenant)."""
    chunks = await (await _get()).knowledge.search("mcp", query, k)
    return [c.to_dict() for c in chunks]


@mcp.tool()
async def list_reviews() -> list[dict[str, Any]]:
    """Answers waiting for a human decision (draft, risk reasons, sources)."""
    return [r.to_dict() for r in await (await _get()).reviews.list("mcp")]


@mcp.tool()
async def resolve_review(
    thread_id: str,
    approved: bool,
    reviewer: str,
    feedback: str | None = None,
    edited_answer: str | None = None,
) -> dict[str, Any]:
    """Approve (optionally with an edited text) or reject a paused answer; the workflow
    resumes from its checkpoint and returns the final result."""
    result = await (await _get()).resolve_review(
        "mcp",
        thread_id,
        approved=approved,
        reviewer=reviewer,
        feedback=feedback,
        edited_answer=edited_answer,
    )
    return result.__dict__


@mcp.tool()
async def crm_alerts() -> list[dict[str, Any]]:
    """Traffic-light follow-ups: unconfirmed appointments, unanswered quotes, recalls due."""
    return [a.to_dict() for a in await (await _get()).crm.alerts("mcp")]


@mcp.tool()
async def insights(question: str | None = None) -> dict[str, Any]:
    """Business metrics computed with SQL (segments, recalls, no-show risk, pipeline,
    forecast). With a `question`, an analyst explains them in natural language."""
    orch = await _get()
    if question:
        return await orch.insights.ask("mcp", question)
    return await orch.insights.summary("mcp")


def main() -> None:
    mcp.run()
