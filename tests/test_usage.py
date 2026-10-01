"""The per-request usage ledger (audit finding A28)."""

from __future__ import annotations

from orchestrator import usage


def test_records_only_inside_a_request() -> None:
    usage.record("llm", "m", input_tokens=5, cost_usd=0.01)  # no request: ignored
    with usage.metered() as entries:
        usage.record("llm", "m", input_tokens=5, output_tokens=2, cost_usd=0.01)
        usage.record("embedding", "e", input_tokens=7, cost_usd=0.0)
    assert len(entries) == 2 and usage.current() is None
    total = usage.summarize(entries)
    assert (total["llm_calls"], total["embedding_calls"]) == (1, 1)
    assert (total["input_tokens"], total["output_tokens"], total["cost_usd"]) == (12, 2, 0.01)
    assert total["cost_status"] == "known"  # 0.0 is a real zero, not unknown


def test_unknown_costs_are_not_reported_as_zero() -> None:
    with usage.metered() as entries:
        usage.record("llm", "local-llama", input_tokens=10, cost_usd=None)
    assert usage.summarize(entries)["cost_status"] == "unknown"
    with usage.metered() as entries:
        usage.record("llm", "claude", cost_usd=0.02)
        usage.record("a2a", "a2a/jvm", cost_usd=None)  # the remote agent's own spend
    total = usage.summarize(entries)
    assert total["cost_status"] == "partial" and total["unpriced_calls"] == 1
    assert total["a2a_calls"] == 1 and total["cost_usd"] == 0.02
