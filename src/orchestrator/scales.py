"""Scoring of standardized questionnaires used in mental-health practice.

PHQ-9 and GAD-7 (Spitzer, Williams, Kroenke and colleagues): the official forms state "No
permission required to reproduce, translate, display or distribute". Severity bands are
those of the original papers (Kroenke et al. 2001; Spitzer et al. 2006), taken from
summaries of them, not re-read in full.

A band is a **severity band, not a diagnosis**: the result is shown to the treating
professional, who interprets it. PHQ-9 item 9 asks about thoughts of being better off dead
or of self-harm; any answer above zero is flagged so a person looks at it, whatever the total.

The patient-facing item texts are not shipped: the official Spanish translation has to be
taken from the publisher's site and checked first (owner's decision P7)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

ScaleId = Literal["phq9", "gad7"]
ITEM_MIN, ITEM_MAX = 0, 3  # "not at all" .. "nearly every day"


@dataclass(frozen=True)
class Scale:
    id: ScaleId
    items: int
    # (lowest total of the band, band name), ascending
    bands: tuple[tuple[int, str], ...]


SCALES: dict[ScaleId, Scale] = {
    "phq9": Scale(
        "phq9",
        9,
        ((0, "minimal"), (5, "mild"), (10, "moderate"), (15, "moderately_severe"), (20, "severe")),
    ),
    "gad7": Scale("gad7", 7, ((0, "minimal"), (5, "mild"), (10, "moderate"), (15, "severe"))),
}
PHQ9_SELF_HARM_ITEM = 9  # 1-based, as printed on the form


@dataclass(frozen=True)
class ScaleResult:
    scale: ScaleId
    total: int
    max_total: int
    band: str
    flags: list[str] = field(default_factory=list)

    @property
    def needs_attention(self) -> bool:
        return bool(self.flags)


def score(scale_id: ScaleId, answers: list[int]) -> ScaleResult:
    scale = SCALES[scale_id]
    if len(answers) != scale.items:
        raise ValueError(f"{scale_id} needs {scale.items} answers, got {len(answers)}")
    if any(
        not isinstance(a, int) or isinstance(a, bool) or not ITEM_MIN <= a <= ITEM_MAX
        for a in answers
    ):
        raise ValueError(f"each answer must be an integer from {ITEM_MIN} to {ITEM_MAX}")
    total = sum(answers)
    band = next(name for low, name in reversed(scale.bands) if total >= low)
    flags = []
    if scale_id == "phq9" and answers[PHQ9_SELF_HARM_ITEM - 1] > 0:
        flags.append("phq9_item9_self_harm_thoughts")
    return ScaleResult(scale_id, total, scale.items * ITEM_MAX, band, flags)
