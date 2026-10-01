"""Industry packs: what changes from one kind of business to another is configuration,
not code. A pack sets which answers need human review, what the CRM pipeline looks like,
when a patient/customer is due for a recall, and which marketing claims are forbidden.

Packs are YAML files in `pack_data/` (shipped with the package). Tenants are mapped to a
pack with TENANT_PACKS; unmapped tenants get DEFAULT_PACK."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, field_validator

from orchestrator.config import Settings

PACKS_DIR = Path(__file__).parent / "pack_data"


class ReviewPolicy(BaseModel):
    # Answers produced by agents of these catalog divisions go to human review.
    divisions: list[str] = Field(default_factory=list)
    # Answers that give or ask for clinical advice (diagnosis, medication, dosage).
    clinical: bool = False


def _all_preferences() -> list[str]:
    from orchestrator.memory import PREFERENCES

    return list(PREFERENCES)


class MemoryPolicy(BaseModel):
    # Which preference keys (orchestrator.memory.PREFERENCES) this business remembers.
    # There is no free-text or clinical memory: health data stays in the clinical record.
    preferences: list[str] = Field(default_factory=_all_preferences)

    @field_validator("preferences")
    @classmethod
    def _known(cls, value: list[str]) -> list[str]:
        from orchestrator.memory import PREFERENCES

        unknown = sorted(set(value) - set(PREFERENCES))
        if unknown:
            raise ValueError(f"unknown memory preferences: {unknown}")
        return value


class CrmPolicy(BaseModel):
    pipeline: list[str] = Field(
        default_factory=lambda: ["presented", "accepted", "in_progress", "completed", "declined"]
    )
    recall_months: int = 12
    # Unconfirmed appointment starting within these hours: yellow, then red.
    confirm_yellow_hours: int = 48
    confirm_red_hours: int = 24
    # Quote/treatment plan presented and not answered after this many days.
    quote_followup_days: int = 7


class CampaignPolicy(BaseModel):
    max_messages_per_month: int = 4
    # Discounts above this need the owner's approval on top of the normal review.
    max_discount_pct: int = 20
    # Lower-cased substrings that may not appear in marketing copy (claims, guarantees).
    banned_claims: list[str] = Field(default_factory=list)
    # Health businesses may not mention clinical details in marketing messages.
    forbid_clinical_terms: bool = False


class Pack(BaseModel):
    id: str
    name: str
    description: str = ""
    review: ReviewPolicy = Field(default_factory=ReviewPolicy)
    memory: MemoryPolicy = Field(default_factory=MemoryPolicy)
    crm: CrmPolicy = Field(default_factory=CrmPolicy)
    campaigns: CampaignPolicy = Field(default_factory=CampaignPolicy)


@lru_cache
def load_packs(directory: Path = PACKS_DIR) -> dict[str, Pack]:
    packs: dict[str, Pack] = {}
    for path in sorted(directory.glob("*.yaml")):
        data: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
        pack = Pack.model_validate(data)
        if pack.id != path.stem:
            raise ValueError(f"{path.name}: id {pack.id!r} must match the file name")
        packs[pack.id] = pack
    return packs


def pack_for(settings: Settings, tenant: str) -> Pack:
    packs = load_packs()
    pack_id = settings.tenant_packs.get(tenant, settings.default_pack)
    if pack_id not in packs:
        raise KeyError(f"unknown pack {pack_id!r} for tenant {tenant!r}")
    return packs[pack_id]


def validate_config(settings: Settings) -> None:
    """Fail at startup, not at the first request of a mis-mapped tenant."""
    packs = load_packs()
    for pack_id in {settings.default_pack, *settings.tenant_packs.values()}:
        if pack_id not in packs:
            raise ValueError(f"unknown pack {pack_id!r}; available: {sorted(packs)}")
