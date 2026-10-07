"""Industry packs: what changes from one kind of business to another is configuration,
not code. A pack sets which answers need human review, what the CRM pipeline looks like,
when a patient/customer is due for a recall, and which marketing claims are forbidden.
Profession packs (ADR 0014) add a jurisdiction with cited legal references, who may
prescribe, a catalog of documents and a safety policy.

Packs are YAML files in `pack_data/` (shipped with the package). A pack may `extends`
another one. Tenants are mapped to a pack with TENANT_PACKS; unmapped tenants get
DEFAULT_PACK. Abstract packs only exist to be extended and cannot be mapped to a tenant."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

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


# --- profession packs (ADR 0014) --------------------------------------------------------

# How well a legal reference is known. `read`: the primary text was read; `secondary`: only a
# summary of it; `to_verify`: not confirmed. A production pack may not rest on `to_verify`.
RefStatus = Literal["read", "secondary", "to_verify"]
DocumentKind = Literal[
    "consent",
    "record",
    "note",
    "psychotherapy_note",
    "patient_entry",
    "referral",
    "report",
    "certificate",
    "prescription",
    "controlled_prescription_worksheet",
]
Access = Literal["care_team", "author_only", "patient_and_treating"]
# Places a document must never reach: shared retrieval, preference memory, insights,
# campaigns, model training or evaluation, and the audit export.
Surface = Literal["rag", "memory", "insights", "campaigns", "models", "audit_export"]

# Surfaces that psychotherapy notes and patient-authored entries are shut out of. A pack may
# add surfaces, never drop these (enforced by `Document`, and across `extends` by `_merge`).
PSYCHOTHERAPY_NOTE_EXCLUDED: tuple[Surface, ...] = (
    "rag",
    "memory",
    "insights",
    "campaigns",
    "models",
    "audit_export",
)
PATIENT_ENTRY_EXCLUDED: tuple[Surface, ...] = ("rag", "memory", "insights", "campaigns", "models")


class LegalRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=40)  # short key, e.g. "LOSM-12"
    instrument: str = Field(min_length=1)  # e.g. "Ley Orgánica de Salud Mental"
    article: str = ""
    status: RefStatus
    note: str = ""


class Jurisdiction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    country: str = Field(pattern=r"^[A-Z]{2}$")  # ISO 3166-1 alpha-2
    refs: list[LegalRef] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique_refs(self) -> Jurisdiction:
        ids = [r.id for r in self.refs]
        duplicated = sorted({i for i in ids if ids.count(i) > 1})
        if duplicated:
            raise ValueError(f"duplicate legal reference ids: {duplicated}")
        return self


class Profession(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # The AI never prescribes; this says whether the *professional* may, so the pack can
    # offer a prescription draft or must offer a referral instead.
    can_prescribe: bool = False
    can_use_controlled_prescription: bool = False
    registry: str = ""  # which licence the professional must hold

    @model_validator(mode="after")
    def _controlled_needs_prescribe(self) -> Profession:
        if self.can_use_controlled_prescription and not self.can_prescribe:
            raise ValueError("can_use_controlled_prescription requires can_prescribe")
        return self


class Document(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=60)
    title: str = Field(min_length=1, max_length=120)
    kind: DocumentKind
    legal_basis: list[str] = Field(default_factory=list)  # ids of `jurisdiction.refs`
    signature: Literal["none", "professional"] = "professional"
    access: Access = "care_team"
    excluded_from: list[Surface] = Field(default_factory=list)
    validity_days: int | None = Field(default=None, ge=1)
    retention_years: int | None = Field(default=None, ge=1)
    note: str = ""

    @model_validator(mode="after")
    def _kind_invariants(self) -> Document:
        """Hard rules about the most sensitive kinds. They are not a default a pack can
        override: a document that breaks them does not load."""
        if self.kind == "psychotherapy_note":
            if self.access != "author_only":
                raise ValueError(f"{self.id}: a psychotherapy note must be author_only")
            missing = sorted(set(PSYCHOTHERAPY_NOTE_EXCLUDED) - set(self.excluded_from))
            if missing:
                raise ValueError(f"{self.id}: a psychotherapy note must be excluded from {missing}")
        if self.kind == "patient_entry":
            if self.access != "patient_and_treating":
                raise ValueError(f"{self.id}: a patient entry must be patient_and_treating")
            if self.signature != "none":
                raise ValueError(f"{self.id}: a patient entry is not signed by a professional")
            missing = sorted(set(PATIENT_ENTRY_EXCLUDED) - set(self.excluded_from))
            if missing:
                raise ValueError(f"{self.id}: a patient entry must be excluded from {missing}")
        return self


class CrisisPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Who is alerted when risk wording appears. The AI never handles the conversation.
    escalate_to: list[str] = Field(
        default_factory=lambda: ["treating_professional", "on_duty_contact"], min_length=1
    )
    # The system never contacts family, emergency services or anyone else by itself: the
    # LOPDP (Arts. 7(6), 26(c), 31(1)) allows vital-interest processing only when the person
    # cannot consent, so a blanket automatic action is not supported. `False` is the only
    # value that loads.
    auto_contact_third_parties: Literal[False] = False


class MinorsPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    age_of_majority: int = Field(default=18, ge=1)
    guardian_consent_required: bool = False
    minor_must_be_heard: bool = False


class Safety(BaseModel):
    model_config = ConfigDict(extra="forbid")

    crisis: CrisisPolicy = Field(default_factory=CrisisPolicy)
    minors: MinorsPolicy = Field(default_factory=MinorsPolicy)


_PRESCRIPTION_KINDS = ("prescription", "controlled_prescription_worksheet")


class Pack(BaseModel):
    model_config = ConfigDict(extra="forbid")  # a typo in a YAML key must not be silently ignored

    id: str
    name: str
    description: str = ""
    extends: str | None = None
    # Abstract packs are only there to be extended; a tenant cannot be mapped to one.
    abstract: bool = False
    # A production pack may not rest on `to_verify` references (`agency pack validate --strict`).
    production: bool = False
    jurisdiction: Jurisdiction | None = None
    profession: Profession = Field(default_factory=Profession)
    documents: list[Document] = Field(default_factory=list)
    safety: Safety = Field(default_factory=Safety)
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

    @model_validator(mode="after")
    def _documents_are_consistent(self) -> Pack:
        ids = [d.id for d in self.documents]
        duplicated = sorted({i for i in ids if ids.count(i) > 1})
        if duplicated:
            raise ValueError(f"duplicate document ids: {duplicated}")
        known = {r.id for r in self.jurisdiction.refs} if self.jurisdiction else set()
        for doc in self.documents:
            unknown = sorted(set(doc.legal_basis) - known)
            if unknown:
                raise ValueError(f"{doc.id}: legal_basis cites unknown references {unknown}")
            if doc.kind in _PRESCRIPTION_KINDS and not self.profession.can_prescribe:
                raise ValueError(f"{doc.id}: this profession cannot prescribe; offer a referral")
            if (
                doc.kind == "controlled_prescription_worksheet"
                and not self.profession.can_use_controlled_prescription
            ):
                raise ValueError(f"{doc.id}: this profession cannot use controlled prescriptions")
        return self

    @model_validator(mode="after")
    def _abstract_is_not_production(self) -> Pack:
        if self.abstract and self.production:
            raise ValueError("an abstract pack cannot be production")
        return self


# --- loading and inheritance ------------------------------------------------------------

# Lists that only ever grow across `extends`: a child cannot lift a parent's prohibition.
_UNION_LISTS = {("campaigns", "banned_claims")}
# Own to each pack, never inherited.
_NOT_INHERITED = {"abstract": False, "production": False}


def _union(parent: list[Any], child: list[Any]) -> list[Any]:
    return [*parent, *(x for x in child if x not in parent)]


def _merge_by_id(
    parent: list[dict[str, Any]], child: list[dict[str, Any]], union_keys: tuple[str, ...] = ()
) -> list[dict[str, Any]]:
    """Merge two lists of mappings keyed by `id`; a child entry overrides the parent's
    fields, except the `union_keys`, which only grow."""
    merged = {item["id"]: dict(item) for item in parent}
    for item in child:
        base = merged.get(item["id"])
        if base is None:
            merged[item["id"]] = dict(item)
            continue
        combined = {**base, **item}
        for key in union_keys:
            combined[key] = _union(base.get(key, []), item.get(key, []))
        merged[item["id"]] = combined
    return list(merged.values())


def _merge(
    parent: dict[str, Any], child: dict[str, Any], path: tuple[str, ...] = ()
) -> dict[str, Any]:
    out = dict(parent)
    for key, value in child.items():
        here = (*path, key)
        if here == ("documents",):
            out[key] = _merge_by_id(parent.get(key, []), value, union_keys=("excluded_from",))
        elif here == ("jurisdiction", "refs"):
            out[key] = _merge_by_id(parent.get(key, []), value)
        elif here in _UNION_LISTS:
            out[key] = _union(parent.get(key, []), value)
        elif isinstance(value, dict) and isinstance(parent.get(key), dict):
            out[key] = _merge(parent[key], value, here)
        else:
            out[key] = value
    return out


def _resolve(
    pack_id: str, raw: dict[str, dict[str, Any]], trail: tuple[str, ...] = ()
) -> dict[str, Any]:
    if pack_id in trail:
        raise ValueError(f"extends cycle: {' -> '.join((*trail, pack_id))}")
    data = raw[pack_id]
    parent_id = data.get("extends")
    if parent_id is None:
        return data
    if parent_id not in raw:
        raise ValueError(f"{pack_id}: extends unknown pack {parent_id!r}")
    merged = _merge(_resolve(parent_id, raw, (*trail, pack_id)), data)
    for key, default in _NOT_INHERITED.items():
        merged[key] = data.get(key, default)
    return merged


@lru_cache
def load_packs(directory: Path = PACKS_DIR) -> dict[str, Pack]:
    raw: dict[str, dict[str, Any]] = {}
    for path in sorted(directory.glob("*.yaml")):
        data: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError(f"{path.name}: must be a mapping")
        if data.get("id") != path.stem:
            raise ValueError(f"{path.name}: id {data.get('id')!r} must match the file name")
        raw[path.stem] = data
    return {pack_id: Pack.model_validate(_resolve(pack_id, raw)) for pack_id in raw}


def pack_for(settings: Settings, tenant: str) -> Pack:
    packs = load_packs()
    pack_id = settings.tenant_packs.get(tenant, settings.default_pack)
    if pack_id not in packs:
        raise KeyError(f"unknown pack {pack_id!r} for tenant {tenant!r}")
    if packs[pack_id].abstract:
        raise KeyError(f"pack {pack_id!r} is abstract and cannot serve tenant {tenant!r}")
    return packs[pack_id]


def validate_config(settings: Settings) -> None:
    """Fail at startup, not at the first request of a mis-mapped tenant."""
    packs = load_packs()
    for pack_id in {settings.default_pack, *settings.tenant_packs.values()}:
        if pack_id not in packs:
            raise ValueError(f"unknown pack {pack_id!r}; available: {sorted(packs)}")
        if packs[pack_id].abstract:
            raise ValueError(f"pack {pack_id!r} is abstract and cannot be mapped to a tenant")


# --- audit helpers (used by `agency pack`) ----------------------------------------------


def unverified_refs(pack: Pack) -> list[str]:
    """Ids of the legal references this pack cites but nobody has confirmed."""
    if pack.jurisdiction is None:
        return []
    return [r.id for r in pack.jurisdiction.refs if r.status == "to_verify"]


def strict_failures(packs: dict[str, Pack]) -> list[str]:
    """Production packs that still rest on unverified legal references."""
    return [
        f"{pack.id}: production pack relies on unverified references {unverified_refs(pack)}"
        for pack in packs.values()
        if pack.production and unverified_refs(pack)
    ]


def summarize(pack: Pack) -> dict[str, Any]:
    return {
        "id": pack.id,
        "name": pack.name,
        "country": pack.jurisdiction.country if pack.jurisdiction else None,
        "extends": pack.extends,
        "abstract": pack.abstract,
        "production": pack.production,
        "documents": len(pack.documents),
        "unverified_refs": len(unverified_refs(pack)),
    }
