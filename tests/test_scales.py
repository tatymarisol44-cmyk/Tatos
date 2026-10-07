"""PHQ-9 and GAD-7 scoring: band edges, input validation, and the PHQ-9 item-9 flag."""

from __future__ import annotations

import pytest

from orchestrator.scales import score


def phq9(total: int, item9: int = 0) -> list[int]:
    """Nine answers summing to `total`, with item 9 fixed."""
    rest = total - item9
    answers = []
    for _ in range(8):
        take = min(3, rest)
        answers.append(take)
        rest -= take
    assert rest == 0
    return [*answers, item9]


@pytest.mark.parametrize(
    ("total", "band"),
    [(0, "minimal"), (4, "minimal"), (5, "mild"), (9, "mild"), (10, "moderate"), (14, "moderate"),
     (15, "moderately_severe"), (19, "moderately_severe"), (20, "severe"), (24, "severe")],
)  # fmt: skip
def test_phq9_band_edges(total: int, band: str) -> None:
    result = score("phq9", phq9(total))
    assert (result.total, result.band, result.max_total) == (total, band, 27)
    assert not result.needs_attention


@pytest.mark.parametrize(
    ("answers", "band"),
    [([0] * 7, "minimal"), ([1, 1, 1, 1, 0, 0, 0], "minimal"), ([1, 1, 1, 1, 1, 0, 0], "mild"),
     ([2, 2, 2, 2, 2, 0, 0], "moderate"), ([3, 3, 3, 3, 3, 0, 0], "severe"), ([3] * 7, "severe")],
)  # fmt: skip
def test_gad7_band_edges(answers: list[int], band: str) -> None:
    result = score("gad7", answers)
    assert result.band == band and result.max_total == 21 and result.flags == []


def test_phq9_item9_is_flagged_whatever_the_total() -> None:
    low = score("phq9", phq9(1, item9=1))
    assert low.band == "minimal"  # a low total is still flagged
    assert low.needs_attention
    assert low.flags == ["phq9_item9_self_harm_thoughts"]


@pytest.mark.parametrize(
    ("scale", "answers"),
    [("phq9", [0] * 8), ("phq9", [0] * 10), ("gad7", [0] * 9), ("phq9", [4] + [0] * 8),
     ("gad7", [-1] + [0] * 6), ("gad7", [True] + [0] * 6)],
)  # fmt: skip
def test_invalid_answers_are_rejected(scale: str, answers: list[int]) -> None:
    with pytest.raises(ValueError):
        score(scale, answers)  # type: ignore[arg-type]
