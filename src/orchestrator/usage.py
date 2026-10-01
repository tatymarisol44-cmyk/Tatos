"""Per-request usage ledger (audit finding A28).

Each model call is recorded where it happens (the LLM client, the embedder, the A2A
call), not reconstructed from the final graph state. That way every call is counted:
- retries and fallbacks;
- calls whose output was discarded (an invalid route, a failed plan);
- memory extraction and query embeddings.

The ledger is a list in a ContextVar, opened around one request with `metered()`. The
graph's tasks inherit a copy of the context that points at the same list, so parallel
team workers are counted too. Outside `metered()` (startup indexing, evals) recording
is a no-op.

A cost is either known, an honest zero (`cost_usd=0.0`), or unknown (`None`), for
example a model without public pricing or a remote agent's own spend. The total says
which, instead of reporting unknown costs as zero."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

_ledger: ContextVar[list[dict[str, Any]] | None] = ContextVar("usage_ledger", default=None)


@contextmanager
def metered() -> Iterator[list[dict[str, Any]]]:
    entries: list[dict[str, Any]] = []
    token = _ledger.set(entries)
    try:
        yield entries
    finally:
        try:
            _ledger.reset(token)
        except ValueError:  # an async generator closed from another context
            _ledger.set(None)


def current() -> list[dict[str, Any]] | None:
    return _ledger.get()


def record(
    kind: str,
    model: str,
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cost_usd: float | None,
) -> None:
    """kind: `llm`, `embedding` or `a2a`."""
    entries = _ledger.get()
    if entries is not None:
        entries.append(
            {
                "kind": kind,
                "model": model,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cost_usd": cost_usd,
            }
        )


def summarize(entries: list[dict[str, Any]]) -> dict[str, Any]:
    unpriced = sum(1 for e in entries if e["cost_usd"] is None)
    return {
        "llm_calls": sum(1 for e in entries if e["kind"] == "llm"),
        "embedding_calls": sum(1 for e in entries if e["kind"] == "embedding"),
        "a2a_calls": sum(1 for e in entries if e["kind"] == "a2a"),
        "input_tokens": sum(e["input_tokens"] for e in entries),
        "output_tokens": sum(e["output_tokens"] for e in entries),
        # Sum of the known costs; `cost_status` says whether that is the whole bill.
        "cost_usd": round(sum(e["cost_usd"] or 0.0 for e in entries), 6),
        "cost_status": "known"
        if not unpriced
        else ("unknown" if unpriced == len(entries) else "partial"),
        "unpriced_calls": unpriced,
    }
