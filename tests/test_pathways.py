"""Contract tests for the workflow routes (evidence gating, query rewrite, citation
verification). They assert which branch ran and what state caused it, not answer quality
(docs/adr/0008)."""

from __future__ import annotations

import pytest

from orchestrator import evidence
from orchestrator.catalog import Catalog
from orchestrator.config import Settings
from orchestrator.llm import FakeLLM
from orchestrator.service import Orchestrator

# --- pure route helpers ---------------------------------------------------------------


def test_grade() -> None:
    assert evidence.grade([], 0.3) == "none"
    assert evidence.grade([{"score": 0.2}, {"score": 0.1}], 0.3) == "weak"
    assert evidence.grade([{"score": 0.2}, {"score": 0.31}], 0.3) == "strong"


def test_contextual_query_needs_a_previous_user_turn() -> None:
    assert evidence.contextual_query([], "and the price?") is None
    history = [
        {"role": "user", "content": "whitening treatment"},
        {"role": "assistant", "content": "..."},
    ]
    assert (
        evidence.contextual_query(history, "and the price?")
        == "whitening treatment\nand the price?"
    )


def test_citations_outside_code_only() -> None:
    answer = "Per [1] and [3].\n```python\nx = items[0] + items[7]\n```\nAlso [2]."
    assert evidence.check_citations(answer, 2) == ([1, 2], [3])
    stripped = evidence.strip_citations(answer, [3])
    assert "Per [1] and ." in stripped
    assert "items[0] + items[7]" in stripped  # code untouched


# --- graph routes --------------------------------------------------------------------


async def _orch(settings: Settings, catalog: Catalog, llm: FakeLLM) -> Orchestrator:
    orch = Orchestrator(settings, catalog=catalog, llm=llm)
    await orch.start()
    await orch.knowledge.add(
        "acme", "Refunds", "Refunds are accepted within 30 days of purchase with a receipt."
    )
    await orch.knowledge.add("acme", "Shipping", "Orders ship within 2 business days.")
    return orch


def _nodes(route_log: list[dict[str, object]]) -> list[object]:
    return [e["node"] for e in route_log]


async def test_strong_evidence_goes_straight_to_the_agent(
    settings: Settings, catalog: Catalog
) -> None:
    orch = await _orch(settings, catalog, FakeLLM())
    result = await orch.chat("refund policy 30 days receipt", tenant="acme")
    assert result.evidence is not None and result.evidence["grade"] == "strong"
    assert _nodes(result.route_log)[:2] == ["grade_evidence", "route"]
    assert result.sources and result.status == "completed"


async def test_weak_evidence_on_a_follow_up_retrieves_again_with_context(
    settings: Settings, catalog: Catalog
) -> None:
    # Everything that matches counts as weak, so the retry branch is taken.
    weak = settings.model_copy(update={"knowledge_strong_score": 0.99, "knowledge_min_score": 0.0})
    orch = await _orch(weak, catalog, FakeLLM())
    await orch.chat("refunds receipt", tenant="acme", thread_id="t")
    follow = await orch.chat("and how many days?", tenant="acme", thread_id="t")
    assert _nodes(follow.route_log)[:4] == [
        "grade_evidence",
        "rewrite_query",
        "grade_evidence",
        "route",
    ]
    assert follow.evidence is not None and follow.evidence["attempts"] == 2
    # On its own "how many days?" matches Shipping ("2 business days"); the rewritten
    # query borrowed "refunds receipt" from the previous turn and found the right one.
    assert follow.sources[0]["title"] == "Refunds"


async def test_weak_evidence_without_history_does_not_retry(
    settings: Settings, catalog: Catalog, fake_llm: FakeLLM
) -> None:
    weak = settings.model_copy(update={"knowledge_strong_score": 0.99, "knowledge_min_score": 0.0})
    orch = await _orch(weak, catalog, fake_llm)
    result = await orch.chat("refund policy", tenant="acme")
    assert "rewrite_query" not in _nodes(result.route_log)
    assert result.evidence is not None and result.evidence["grade"] == "weak"
    # The agent was told the excerpts are only loosely related.
    assert "only loosely related" in fake_llm.calls[-1][-1]["content"]


async def test_no_documents_means_no_evidence(orchestrator: Orchestrator) -> None:
    result = await orchestrator.chat("kubernetes docker")
    assert result.evidence == {"grade": "none", "top_score": 0.0, "attempts": 1}


async def test_invented_citation_is_regenerated(settings: Settings, catalog: Catalog) -> None:
    llm = FakeLLM(agent_replies=["Refunds: see [1] and [7].", "Refunds within 30 days [1]."])
    orch = await _orch(settings, catalog, llm)
    result = await orch.chat("refund policy 30 days receipt", tenant="acme")
    assert result.answer == "Refunds within 30 days [1]."
    assert result.citations == {"used": [1], "invalid": []}
    verdicts = [e["decision"] for e in result.route_log if e["node"] == "verify_citations"]
    assert verdicts == ["regenerate", "ok"]
    # The regeneration was told exactly what was wrong.
    assert "[7]" in llm.calls[-1][-1]["content"]


async def test_citations_that_stay_invalid_are_stripped(
    settings: Settings, catalog: Catalog
) -> None:
    llm = FakeLLM(agent_replies=["See [9].", "Still [9] and [1]."])
    orch = await _orch(settings, catalog, llm)
    result = await orch.chat("refund policy 30 days receipt", tenant="acme")
    assert result.answer == "Still  and [1]."
    assert "citation:invalid" in result.guardrails["flags"]
    assert result.citations == {"used": [1], "invalid": [9]}


async def test_brackets_without_sources_are_not_citations(
    orchestrator: Orchestrator, fake_llm: FakeLLM
) -> None:
    fake_llm.agent_replies = ["Use arr[3] to read the fourth item."]
    result = await orchestrator.chat("python arrays")
    assert result.answer == "Use arr[3] to read the fourth item."
    assert result.citations == {"used": [], "invalid": []}


async def test_team_citations_are_stripped_without_retry(
    settings: Settings, catalog: Catalog
) -> None:
    llm = FakeLLM()
    orch = await _orch(settings, catalog, llm)
    llm.agent_replies = ["step one [1]", "step two [5]"]
    result = await orch.chat("refund policy 30 days receipt", tenant="acme", mode="team")
    verdicts = [e["decision"] for e in result.route_log if e["node"] == "verify_citations"]
    assert verdicts in (["ok"], ["stripped"])
    assert "[5]" not in (result.answer or "")


@pytest.mark.parametrize("mode", ["single", "team"])
async def test_route_log_records_every_decision(orchestrator: Orchestrator, mode: str) -> None:
    result = await orchestrator.chat("kubernetes docker", mode=mode)  # type: ignore[arg-type]
    nodes = _nodes(result.route_log)
    assert nodes[0] == "grade_evidence"
    assert ("route" if mode == "single" else "plan") in nodes
    assert nodes[-2:] == ["verify_citations", "risk_score"]
    assert result.decision_record is not None and result.decision_record["status"] == "completed"
