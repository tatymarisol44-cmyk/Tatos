"""What each caller is allowed to see of a chat result (audit finding A02).

A held answer is a draft for a reviewer, not for whoever asked: callers without the
reviewer role never receive the draft, the specialists' raw contributions while the
answer is held (or after it was rejected), or the reviewer's private text. Patients get
an even narrower shape (`Orchestrator.patient_view`)."""

from __future__ import annotations

from typing import Any

from orchestrator.auth import Principal


def _without_output(step: dict[str, Any]) -> dict[str, Any]:
    return {**step, "output": None}


def staff_view(result: dict[str, Any], principal: Principal) -> dict[str, Any]:
    if principal.can_review:
        return result
    data = dict(result)
    review = data.get("review")
    if review:
        data["review"] = {"risk": review.get("risk")}
    team = data.get("team")
    if team and data.get("status") != "completed":
        data["team"] = {
            "plan": team.get("plan"),
            "results": [_without_output(r) for r in team.get("results", [])],
        }
    record = data.get("decision_record")
    if record and record.get("review"):
        decision = record["review"]
        data["decision_record"] = {
            **record,
            "review": {"approved": decision.get("approved"), "reviewer": decision.get("reviewer")},
        }
    return data


def stream_event(event: str, data: dict[str, Any], principal: Principal) -> dict[str, Any]:
    """Filter one SSE event. While streaming nobody knows yet whether the answer will be
    held, so non-reviewers get step progress (agent, duration, error) without the text."""
    if principal.can_review:
        return data
    if event == "step":
        return _without_output(data)
    if event == "review":
        return {"status": data.get("status"), "risk": data.get("risk")}
    if event == "done":
        return staff_view(data, principal)
    return data
