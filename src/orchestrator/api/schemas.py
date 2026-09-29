from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


class RouteRequest(BaseModel):
    question: str = Field(min_length=1, max_length=20_000)


class ChatRequest(BaseModel):
    question: str = Field(min_length=1, max_length=20_000)
    thread_id: str | None = Field(default=None, max_length=128, pattern=r"^[\w-]+$")
    mode: Literal["single", "team"] = Field(
        default="single",
        description="`single`: route to one specialist. `team`: a planner splits the request "
        "across several specialists, runs them in parallel and synthesizes one answer.",
    )
    agent_id: str | None = Field(
        default=None, description="single mode: skip routing and talk to this agent directly."
    )
    agent_ids: list[str] | None = Field(
        default=None,
        max_length=8,
        description="team mode: build the team from exactly these agents.",
    )

    @model_validator(mode="after")
    def _check_mode(self) -> ChatRequest:
        if self.agent_ids and self.mode != "team":
            raise ValueError("agent_ids requires mode='team'")
        if self.agent_id and self.mode != "single":
            raise ValueError("agent_id requires mode='single'; use agent_ids for a team")
        return self


class ChatResponse(BaseModel):
    thread_id: str
    blocked: bool
    answer: str | None
    mode: Literal["single", "team"] = "single"
    routing: dict[str, Any] | None
    team: dict[str, Any] | None = None
    sources: list[dict[str, Any]] = Field(
        default_factory=list, description="Company documents cited as [n] in the answer."
    )
    guardrails: dict[str, list[str]]
    usage: dict[str, Any]


class DocumentIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    text: str = Field(min_length=1, max_length=1_000_000)
    doc_id: str | None = Field(
        default=None,
        max_length=64,
        pattern=r"^[\w.-]+$",
        description="Reuse an id to replace a document; omit to create a new one.",
    )


class DocumentOut(BaseModel):
    doc_id: str
    title: str
    chunks: int


class KnowledgeSearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    k: int = Field(default=4, ge=1, le=20)


class AgentSummary(BaseModel):
    id: str
    name: str
    division: str
    description: str
    emoji: str = ""
