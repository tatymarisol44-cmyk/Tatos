"""Minimal Agent2Agent (A2A) server: an Agent Card plus JSON-RPC `message/send`.

Other agents (e.g. a Spring AI or Semantic Kernel service) can discover this
orchestrator via the card and delegate questions to it. `contextId` maps to the
orchestrator's thread id, so multi-turn A2A conversations keep their history."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Request

from orchestrator import __version__
from orchestrator.api.security import require_tenant
from orchestrator.service import Orchestrator

router = APIRouter()


def agent_card(orch: Orchestrator) -> dict[str, Any]:
    base = orch.settings.public_base_url.rstrip("/")
    by_division: dict[str, list[str]] = {}
    for agent in orch.catalog.agents.values():
        by_division.setdefault(agent.division, []).append(agent.name)
    return {
        "protocolVersion": "0.3.0",
        "name": "Agency Orchestrator",
        "description": (
            f"Routes any request to the best of {len(orch.catalog)} specialist agents "
            "(engineering, marketing, security, design, finance, ...), or orchestrates a "
            'team of them when the message metadata has {"mode": "team"}.'
        ),
        "url": f"{base}/a2a",
        "preferredTransport": "JSONRPC",
        "version": __version__,
        "capabilities": {"streaming": False, "pushNotifications": False},
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["text/plain"],
        "securitySchemes": {"apiKey": {"type": "apiKey", "in": "header", "name": "X-API-Key"}},
        "security": [{"apiKey": []}],
        "skills": [
            {
                "id": division,
                "name": division.replace("-", " ").title(),
                "description": "Specialists: " + ", ".join(sorted(names)[:12]),
                "tags": [division],
            }
            for division, names in sorted(by_division.items())
        ],
    }


@router.get("/.well-known/agent-card.json")
@router.get("/.well-known/agent.json", include_in_schema=False)
async def get_agent_card(request: Request) -> dict[str, Any]:
    return agent_card(request.app.state.orchestrator)


def _rpc_error(req_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


@router.post("/a2a")
async def a2a_rpc(
    request: Request, body: dict[str, Any], tenant: str = Depends(require_tenant)
) -> dict[str, Any]:
    req_id = body.get("id")
    if body.get("jsonrpc") != "2.0" or "method" not in body:
        return _rpc_error(req_id, -32600, "Invalid Request")
    if body["method"] != "message/send":
        return _rpc_error(req_id, -32601, f"Method not found: {body['method']}")
    message = (body.get("params") or {}).get("message") or {}
    text = "\n".join(
        p.get("text", "") for p in message.get("parts", []) if p.get("kind") == "text"
    ).strip()
    if not text:
        return _rpc_error(req_id, -32602, "message must contain at least one text part")

    orch: Orchestrator = request.app.state.orchestrator
    context_id = str(message.get("contextId") or uuid.uuid4())
    # Callers opt into team orchestration with message metadata {"mode": "team"}.
    team = (message.get("metadata") or {}).get("mode") == "team"
    result = await orch.chat(
        text, thread_id=context_id, mode="team" if team else "single", tenant=tenant
    )
    reply = (
        result.answer
        if not result.blocked
        else "Request blocked by guardrails: " + ", ".join(result.guardrails["reasons"])
    )
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "result": {
            "kind": "message",
            "role": "agent",
            "messageId": str(uuid.uuid4()),
            "contextId": context_id,
            "parts": [{"kind": "text", "text": reply or ""}],
            "metadata": {
                "mode": result.mode,
                "routing": result.routing,
                "team": result.team["plan"] if result.team else None,
                "blocked": result.blocked,
            },
        },
    }
