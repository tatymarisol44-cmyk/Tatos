"""Risk rules of the ERM module: when an answer needs a human, and whether marketing copy
is compliant. Deterministic on purpose: a reviewer (or an auditor) must be able to tell
exactly why something was held back, and the rules are covered by unit tests."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

from orchestrator.packs import Pack

# Clinical advice: diagnosis, prescriptions, medication and dosage (EN/ES). Matched on
# accent-folded, lower-cased text.
_CLINICAL = re.compile(
    r"\b("
    r"diagnos\w*|prescri\w*|receta\w*|dos(is|age|e)|posolog\w*|"
    r"medica(tion|mento|mentos|cion)\w*|antibiotic\w*|antibiotico\w*|"
    r"amoxicilin\w*|amoxicillin\w*|ibuprofen\w*|paracetamol|acetaminophen|"
    r"analgesic\w*|analgesico\w*|anestesi\w*|anesthe\w*|opioid\w*|"
    r"what should i take|que (debo|puedo) tomar|cuanto (debo )?tomar"
    r")\b",
    re.IGNORECASE,
)
# Clinical details that must not appear in marketing messages to patients.
_CLINICAL_DETAIL = re.compile(
    r"\b("
    r"root canal|endodonc\w*|conducto|extraction|extraccion|implant\w*|"
    r"periodont\w*|gingivitis|caries|cavity|cavities|infection|infeccion|"
    r"abscess|absceso|orthodont\w*|ortodonc\w*|biopsy|biopsia|diagnos\w*|"
    r"treatment plan|plan de tratamiento|tu tratamiento|your treatment"
    r")\b",
    re.IGNORECASE,
)
_PERCENT = re.compile(r"(\d{1,3})\s?%")


def fold(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text.lower())
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def is_clinical(text: str) -> bool:
    return bool(_CLINICAL.search(fold(text)))


@dataclass
class RiskAssessment:
    level: str  # "high" | "low"
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"level": self.level, "reasons": self.reasons}


def assess(
    pack: Pack,
    *,
    question: str,
    answer: str,
    divisions: list[str],
    flags: list[str],
    forced: bool = False,
) -> RiskAssessment:
    reasons: list[str] = []
    if forced:
        reasons.append("forced")
    if "prompt_injection" in flags:
        # Only reachable with INJECTION_ACTION=flag: the answer was produced anyway.
        reasons.append("prompt_injection_flagged")
    reasons.extend(f"division:{d}" for d in sorted(set(divisions) & set(pack.review.divisions)))
    if pack.review.clinical and (is_clinical(question) or is_clinical(answer)):
        reasons.append("clinical_advice")
    return RiskAssessment("high" if reasons else "low", reasons)


@dataclass
class CopyCheck:
    ok: bool
    violations: list[str] = field(default_factory=list)
    # Allowed, but the owner has to sign off on top of the normal review.
    needs_owner_approval: bool = False

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def check_copy(text: str, pack: Pack) -> CopyCheck:
    """Marketing copy rules of the pack: banned claims, clinical details, discount cap."""
    folded = fold(text)
    violations = [
        f"banned_claim:{claim}" for claim in pack.campaigns.banned_claims if fold(claim) in folded
    ]
    if pack.campaigns.forbid_clinical_terms:
        violations.extend(
            f"clinical_detail:{m}"
            for m in sorted({m.group(0) for m in _CLINICAL_DETAIL.finditer(folded)})
        )
    discounts = [int(p) for p in _PERCENT.findall(folded)]
    over_cap = any(d > pack.campaigns.max_discount_pct for d in discounts)
    return CopyCheck(ok=not violations, violations=violations, needs_owner_approval=over_cap)
