"""What is shown with an answer, shared by staff and patient views: its provenance and
its cited sources. No dependencies on the services that produce answers."""

from __future__ import annotations

from typing import Any, Literal

PATIENT_PENDING = (
    "Your question needs a review by a professional at the clinic. You will see the answer "
    "here as soon as they have reviewed it."
)
PATIENT_BLOCKED = "We could not process this message. Please rephrase it."


Provenance = Literal[
    "ai_unreviewed", "ai_pending_review", "professional_approved", "professional_edited", "none"
]


def provenance(status: str, review_decision: dict[str, Any] | None) -> Provenance:
    """Who stands behind what is shown, so no interface can present an AI answer as a
    professional's assessment (audit 2026-10-08, UI): an unreviewed AI answer, a draft a
    professional has not decided yet, an answer a professional approved as is or edited.
    `none` when nothing is shown (blocked, rejected)."""
    if status == "pending_review":
        return "ai_pending_review"
    if status != "completed":
        return "none"
    if review_decision and review_decision.get("approved"):
        return (
            "professional_edited"
            if review_decision.get("edited_answer")
            else ("professional_approved")
        )
    return "ai_unreviewed"


def sources_view(chunks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Citation list matching the [n] markers the agents were asked to use."""
    return [
        {
            "n": i,
            "doc_id": c["doc_id"],
            "title": c["title"],
            "score": round(c["score"], 4),
            "excerpt": c["text"][:300],
        }
        for i, c in enumerate(chunks, 1)
    ]
