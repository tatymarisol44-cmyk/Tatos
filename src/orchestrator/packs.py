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
from pydantic import BaseModel, Field, field_validator, model_validator

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


class ConsentPrompt(BaseModel):
    """How the app asks for one consent: what the patient gains, in plain words. The
    pack writes the pitch; the rules that make it a free choice are not configurable
    (`CONSENT_FOOTER`, equal options, nothing pre-selected)."""

    title: str = Field(min_length=1, max_length=80)
    benefit: str = Field(min_length=1, max_length=300)  # what the patient gets
    detail: str = Field(min_length=1, max_length=400)  # what is and is not done with it
    yes_label: str = Field(default="Sí, acepto", max_length=40)
    no_label: str = Field(default="No, gracias", max_length=40)


# Purposes the app asks about, in this order (treatment rests on another legal basis).
PROMPTED_PURPOSES = ("marketing", "analytics", "memory")
# Shown under every consent question, whatever the pack says: consent tied to the service
# is not freely given (GDPR Art. 7(4)), so the app must say that care never depends on it.
CONSENT_FOOTER = (
    "Es voluntario: tu atención y tus citas no dependen de tu respuesta. "
    "Puedes cambiarla cuando quieras en Mi perfil."
)


def _default_prompts() -> dict[str, ConsentPrompt]:
    return {
        "marketing": ConsentPrompt(
            title="Recordatorios y beneficios",
            benefit="Te avisamos cuando te toque volver y te enviamos ofertas pensadas "
            "para ti, como máximo unas pocas veces al mes.",
            detail="Primero en la app; por Telegram solo si no la abres. Responde STOP "
            "en cualquier momento para dejar de recibirlos.",
        ),
        "analytics": ConsentPrompt(
            title="Ofertas que sí te sirvan",
            benefit="Usamos tu historial de visitas para proponerte lo que de verdad "
            "necesitas, y no promociones al azar.",
            detail="Solo dentro de este negocio. Nunca vendemos ni compartimos tus datos.",
        ),
        "memory": ConsentPrompt(
            title="Que el asistente te recuerde",
            benefit="El asistente recuerda tus preferencias (horario, canal, idioma) "
            "para que no tengas que repetirlas.",
            detail="Solo preferencias de una lista cerrada; nunca datos de salud.",
        ),
    }


class Pack(BaseModel):
    id: str
    name: str
    description: str = ""
    review: ReviewPolicy = Field(default_factory=ReviewPolicy)
    memory: MemoryPolicy = Field(default_factory=MemoryPolicy)
    crm: CrmPolicy = Field(default_factory=CrmPolicy)
    campaigns: CampaignPolicy = Field(default_factory=CampaignPolicy)
    consent_prompts: dict[str, ConsentPrompt] = Field(default_factory=_default_prompts)

    @field_validator("consent_prompts")
    @classmethod
    def _every_prompted_purpose(cls, value: dict[str, ConsentPrompt]) -> dict[str, ConsentPrompt]:
        missing = sorted(set(PROMPTED_PURPOSES) - set(value))
        extra = sorted(set(value) - set(PROMPTED_PURPOSES))
        if missing or extra:
            raise ValueError(f"consent_prompts: missing {missing}, unknown {extra}")
        return value

    @model_validator(mode="after")
    def _honest_prompts(self) -> Pack:
        """The consent pitch obeys the same advertising rules as the campaigns."""
        for purpose, prompt in self.consent_prompts.items():
            text = f"{prompt.title} {prompt.benefit} {prompt.detail}".lower()
            banned = [c for c in self.campaigns.banned_claims if c.lower() in text]
            if banned:
                raise ValueError(f"consent_prompts.{purpose}: banned claims {banned}")
        return self


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
