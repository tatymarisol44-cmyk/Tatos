"""Evidence gating and citation checks for the RAG path.

Weak evidence and invented citations are *routes* in the graph, not instructions hidden
in a prompt: the grade decides whether to retrieve again, and a citation that points to
no retrieved excerpt sends the answer back for regeneration or gets stripped
(fail closed). These are plain functions so the routes can be tested without an LLM."""

from __future__ import annotations

import re
from typing import Any, Literal

Grade = Literal["strong", "weak", "none"]

_MARKER = re.compile(r"\[(\d{1,3})\]")
# Fenced code blocks: "[0]" there is an index, not a citation.
_FENCE = re.compile(r"(```.*?```)", re.DOTALL)


def grade(chunks: list[dict[str, Any]], strong_score: float) -> Grade:
    if not chunks:
        return "none"
    return "strong" if max(c["score"] for c in chunks) >= strong_score else "weak"


def contextual_query(history: list[dict[str, str]], question: str) -> str | None:
    """Follow-ups ("and how much does it cost?") retrieve poorly on their own: prefix the
    previous user turn. None when there is no previous turn to borrow context from."""
    previous = [m["content"] for m in history if m.get("role") == "user"]
    if not previous:
        return None
    return f"{previous[-1]}\n{question}"


def _prose(answer: str) -> list[tuple[bool, str]]:
    """Split into (is_code, text) segments."""
    return [(part.startswith("```"), part) for part in _FENCE.split(answer) if part]


def check_citations(answer: str, n_sources: int) -> tuple[list[int], list[int]]:
    """Return (used, invalid) citation numbers found outside code blocks."""
    found: set[int] = set()
    for is_code, text in _prose(answer):
        if not is_code:
            found.update(int(m) for m in _MARKER.findall(text))
    used = sorted(n for n in found if 1 <= n <= n_sources)
    invalid = sorted(n for n in found if not 1 <= n <= n_sources)
    return used, invalid


def strip_citations(answer: str, invalid: list[int]) -> str:
    bad = set(invalid)
    return "".join(
        text
        if is_code
        else _MARKER.sub(lambda m: "" if int(m.group(1)) in bad else m.group(0), text)
        for is_code, text in _prose(answer)
    )
