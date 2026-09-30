from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

# Pseudonymous data-subject id (patient/customer number): never a name or an e-mail.
SUBJECT_ID = r"^[\w.-]{1,64}$"


class RouteRequest(BaseModel):
    question: str = Field(min_length=1, max_length=20_000)


class ChatRequest(BaseModel):
    question: str = Field(min_length=1, max_length=20_000)
    thread_id: str | None = Field(default=None, max_length=128, pattern=r"^[\w-]+$")
    subject_id: str | None = Field(
        default=None,
        pattern=SUBJECT_ID,
        description="Pseudonymous id of the customer/patient the conversation is about. "
        "Enables long-term memory (with the `memory` consent) and ties the audit trail "
        "to the subject.",
    )
    force_review: bool = Field(
        default=False, description="Send the answer to human review whatever its risk."
    )
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
    status: Literal["completed", "blocked", "pending_review", "rejected"] = "completed"
    blocked: bool
    answer: str | None = Field(description="None while the answer waits for human review.")
    mode: Literal["single", "team"] = "single"
    routing: dict[str, Any] | None
    team: dict[str, Any] | None = None
    sources: list[dict[str, Any]] = Field(
        default_factory=list, description="Company documents cited as [n] in the answer."
    )
    guardrails: dict[str, list[str]]
    usage: dict[str, Any]
    review: dict[str, Any] | None = Field(
        default=None, description="While pending: the draft, risk reasons and sources."
    )
    evidence: dict[str, Any] | None = None
    citations: dict[str, Any] | None = None
    route_log: list[dict[str, Any]] = Field(
        default_factory=list, description="Each decision the workflow took, and why."
    )
    decision_record: dict[str, Any] | None = None
    memory: dict[str, int] = Field(default_factory=dict)


class ReviewDecision(BaseModel):
    approved: bool
    feedback: str | None = Field(default=None, max_length=2000)
    edited_answer: str | None = Field(
        default=None, max_length=20_000, description="Replace the draft with this text."
    )


class ConsentIn(BaseModel):
    granted: bool
    source: str = Field(
        min_length=1,
        max_length=128,
        description="Where the consent was collected, e.g. 'signed-form-2026-09', 'web'.",
    )


class PatientIn(BaseModel):
    id: str | None = Field(default=None, pattern=SUBJECT_ID)
    display_name: str = Field(min_length=1, max_length=200)
    phone: str | None = Field(default=None, max_length=32)
    email: str | None = Field(default=None, max_length=254)
    telegram_chat_id: str | None = Field(default=None, max_length=64, pattern=r"^-?\d+$")
    birth_date: date | None = None
    preferred_channel: Literal["telegram", "phone", "email"] | None = None


class PatientPatch(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=200)
    phone: str | None = Field(default=None, max_length=32)
    email: str | None = Field(default=None, max_length=254)
    telegram_chat_id: str | None = Field(default=None, max_length=64, pattern=r"^-?\d+$")
    birth_date: date | None = None
    preferred_channel: Literal["telegram", "phone", "email"] | None = None


class AppointmentIn(BaseModel):
    patient_id: str = Field(pattern=SUBJECT_ID)
    starts_at: datetime
    duration_min: int = Field(default=30, ge=5, le=480)
    kind: str = Field(default="checkup", min_length=1, max_length=32)
    price: float = Field(default=0.0, ge=0, le=1_000_000)


class AppointmentStatusIn(BaseModel):
    status: Literal["confirmed", "completed", "no_show", "cancelled"]


class TreatmentIn(BaseModel):
    patient_id: str = Field(pattern=SUBJECT_ID)
    title: str = Field(min_length=1, max_length=200)
    amount: float = Field(ge=0, le=10_000_000)


class TreatmentStageIn(BaseModel):
    stage: str = Field(min_length=1, max_length=16)


class InsightsQuestion(BaseModel):
    question: str = Field(min_length=1, max_length=2000)


class CampaignIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    kind: Literal[
        "recall", "reactivation", "pending_treatment", "referral", "birthday", "education"
    ]
    segment: str = Field(
        min_length=1,
        max_length=32,
        description="An insights segment (at_risk, dormant, ...), recall_due or pending_treatment.",
    )
    channel: Literal["telegram"] = "telegram"
    template: str | None = Field(
        default=None,
        max_length=1000,
        description="Message text; {first_name} is the only placeholder. Omit to let the "
        "copywriter draft it.",
    )
    holdout_pct: int | None = Field(default=None, ge=0, le=50)
    language: str = Field(default="es", pattern=r"^[a-z]{2}$")


class CampaignTemplateIn(BaseModel):
    template: str = Field(min_length=1, max_length=1000)


class CampaignApproveIn(BaseModel):
    owner_approval: bool = Field(
        default=False, description="Required when a discount exceeds the pack's cap."
    )


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
    remote: bool = False
