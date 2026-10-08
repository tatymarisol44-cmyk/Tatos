"""Psychological instruments defined by the professional (tests, scales, questionnaires).

A psychologist can bring any instrument they use: items (choice, number or open text),
reverse-scored items, sum or mean scoring, subscales, interpretation bands and alert
rules ("item 9 >= 1: a person must look at this now"). Two public-domain templates
(PHQ-9, GAD-7) come ready; everything else is the professional's own.

- **Versioned.** Editing an instrument writes a new version; a result keeps the exact
  version it was scored with, so a score can always be reproduced.
- **Licensing.** Many published tests are copyrighted. The professional attests that
  they have the right to use the instrument; the system ships no copyrighted item text.
- **Not a diagnosis.** A band is an aid for the treating professional, who interprets it.
- **Data class.** Definitions hold no patient data (INTERNAL). Results are health data
  (HEALTH): clinicians only, audited, in the patient's export, never in RAG, campaigns
  or model calls (classification.py).
"""

from __future__ import annotations

import re
import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    Float,
    Integer,
    String,
    Table,
    and_,
    insert,
    or_,
    select,
    update,
)

from orchestrator.auth import Principal, Role
from orchestrator.crm import patients
from orchestrator.db import Database, metadata, utcnow
from orchestrator.governance import AuditLog
from orchestrator.scales import PHQ9_SELF_HARM_ITEM, SCALES

ITEM_ID = r"^[\w-]{1,32}$"
Severity = Literal["none", "low", "moderate", "high"]


class InstrumentError(ValueError):
    """An invalid definition, an incomplete or out-of-range answer, an unknown patient."""


# --- the definition ---------------------------------------------------------------------


class Option(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str = Field(min_length=1, max_length=200)
    value: float


class Item(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=ITEM_ID)
    text: str = Field(min_length=1, max_length=1000)
    type: Literal["choice", "number", "text"] = "choice"
    options: list[Option] = Field(default_factory=list, max_length=20)
    min: float | None = None  # number items
    max: float | None = None
    reverse: bool = False  # choice items: scored as (lowest + highest) - value
    scored: bool = True
    required: bool = True

    @model_validator(mode="after")
    def _shape(self) -> Item:
        if self.type == "choice":
            if len(self.options) < 2:
                raise ValueError(f"item {self.id}: a choice item needs at least 2 options")
            if len({o.value for o in self.options}) != len(self.options):
                raise ValueError(f"item {self.id}: option values must be distinct")
        elif self.options:
            raise ValueError(f"item {self.id}: only choice items have options")
        if self.type == "text" and self.scored:
            self.scored = False  # open answers are recorded, never scored
        if (
            self.type == "number"
            and self.min is not None
            and self.max is not None
            and self.min > self.max
        ):
            raise ValueError(f"item {self.id}: min is above max")
        if self.reverse and self.type != "choice":
            raise ValueError(f"item {self.id}: only choice items can be reverse-scored")
        return self


class Band(BaseModel):
    model_config = ConfigDict(extra="forbid")
    min: float
    max: float
    label: str = Field(min_length=1, max_length=80)
    severity: Severity = "none"


class AlertRule(BaseModel):
    model_config = ConfigDict(extra="forbid")
    item: str = Field(pattern=ITEM_ID)
    op: Literal["gte", "lte", "eq"] = "gte"
    value: float
    message: str = Field(min_length=1, max_length=300)


class Scoring(BaseModel):
    model_config = ConfigDict(extra="forbid")
    method: Literal["sum", "mean", "none"] = "sum"
    subscales: dict[str, list[str]] = Field(default_factory=dict)
    bands: list[Band] = Field(default_factory=list, max_length=20)
    alerts: list[AlertRule] = Field(default_factory=list, max_length=50)


class Licence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: str = Field(min_length=1, max_length=300, description="Author, publisher or 'own'")
    public_domain: bool = False
    attestation: bool = Field(
        description="The professional has the right to use this instrument in their practice"
    )
    note: str = Field(default="", max_length=500)

    @model_validator(mode="after")
    def _attested(self) -> Licence:
        if not self.attestation:
            raise ValueError("confirm that you have the right to use this instrument")
        return self


class InstrumentSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=2000)
    instructions: str = Field(default="", max_length=2000)
    administration: Literal["professional", "self_report"] = "professional"
    items: list[Item] = Field(min_length=1, max_length=200)
    scoring: Scoring = Field(default_factory=Scoring)
    licence: Licence

    @model_validator(mode="after")
    def _references(self) -> InstrumentSpec:
        ids = [i.id for i in self.items]
        if len(set(ids)) != len(ids):
            raise ValueError("item ids must be unique")
        known = set(ids)
        for name, members in self.scoring.subscales.items():
            if not re.match(ITEM_ID, name):
                raise ValueError(f"subscale name {name!r} is not valid")
            if missing := set(members) - known:
                raise ValueError(f"subscale {name}: unknown items {sorted(missing)}")
        for rule in self.scoring.alerts:
            if rule.item not in known:
                raise ValueError(f"alert on unknown item {rule.item!r}")
        for band in self.scoring.bands:
            if band.min > band.max:
                raise ValueError(f"band {band.label!r}: min is above max")
        if self.scoring.method != "none" and not any(i.scored for i in self.items):
            raise ValueError("nothing to score: mark an item as scored or use method 'none'")
        return self


# --- scoring ----------------------------------------------------------------------------


def _item_value(item: Item, raw: Any) -> float | str:
    if item.type == "text":
        if not isinstance(raw, str) or len(raw) > 4000:
            raise InstrumentError(f"item {item.id}: expected a text of up to 4000 characters")
        return raw
    if isinstance(raw, bool) or not isinstance(raw, int | float):
        raise InstrumentError(f"item {item.id}: expected a number")
    value = float(raw)
    if item.type == "choice":
        if value not in {o.value for o in item.options}:
            raise InstrumentError(f"item {item.id}: {raw} is not one of its options")
    else:
        if (item.min is not None and value < item.min) or (
            item.max is not None and value > item.max
        ):
            raise InstrumentError(f"item {item.id}: {raw} is out of range")
    return value


def _scored(item: Item, value: float) -> float:
    if item.reverse:
        values = [o.value for o in item.options]
        return min(values) + max(values) - value
    return value


def _aggregate(method: str, values: list[float]) -> float | None:
    if method == "none" or not values:
        return None
    total = sum(values)
    return round(total / len(values), 4) if method == "mean" else round(total, 4)


def score(spec: InstrumentSpec, answers: dict[str, Any]) -> dict[str, Any]:
    """Validate the answers against the definition and score them. Raises InstrumentError."""
    items = {i.id: i for i in spec.items}
    if unknown := set(answers) - set(items):
        raise InstrumentError(f"answers for unknown items: {sorted(unknown)}")
    clean: dict[str, float | str] = {}
    for item in spec.items:
        if item.id not in answers or answers[item.id] is None:
            if item.required:
                raise InstrumentError(f"item {item.id} needs an answer")
            continue
        clean[item.id] = _item_value(item, answers[item.id])
    scored = {
        i.id: _scored(i, float(clean[i.id]))
        for i in spec.items
        if i.scored and i.id in clean and not isinstance(clean[i.id], str)
    }
    total = _aggregate(spec.scoring.method, list(scored.values()))
    subscales = {
        name: _aggregate(spec.scoring.method, [scored[m] for m in members if m in scored])
        for name, members in spec.scoring.subscales.items()
    }
    band = next(
        (b for b in spec.scoring.bands if total is not None and b.min <= total <= b.max), None
    )
    alerts = []
    for rule in spec.scoring.alerts:
        raw = clean.get(rule.item)
        if isinstance(raw, str) or raw is None:
            continue
        hit = {"gte": raw >= rule.value, "lte": raw <= rule.value, "eq": raw == rule.value}
        if hit[rule.op]:
            alerts.append({"item": rule.item, "message": rule.message})
    return {
        "answers": clean,
        "total": total,
        "subscales": subscales,
        "band": band.label if band else None,
        "severity": band.severity if band else None,
        "alerts": alerts,
    }


# --- ready-made templates (public domain only) ------------------------------------------

FREQUENCY_ES = ["Nunca", "Varios días", "Más de la mitad de los días", "Casi todos los días"]
SEVERITY = {
    "minimal": "none",
    "mild": "low",
    "moderate": "moderate",
    "moderately_severe": "high",
    "severe": "high",
}


def _template(scale_id: Literal["phq9", "gad7"], name: str) -> InstrumentSpec:
    scale = SCALES[scale_id]
    lows = [low for low, _ in scale.bands]
    highs = [*[low - 1 for low in lows[1:]], scale.items * 3]
    alerts = []
    if scale_id == "phq9":
        alerts.append(
            AlertRule(
                item=f"i{PHQ9_SELF_HARM_ITEM}",
                op="gte",
                value=1,
                message="Ítem de autolesión con respuesta mayor que 0: revisar hoy.",
            )
        )
    return InstrumentSpec(
        name=name,
        description="Plantilla de dominio público. Banda de severidad, no diagnóstico.",
        instructions=(
            "Durante las últimas 2 semanas, ¿con qué frecuencia le han molestado los "
            "siguientes problemas? Copie el texto oficial de cada ítem del formulario "
            "publicado (decisión P7)."
        ),
        items=[
            Item(
                id=f"i{n}",
                text=f"Ítem {n} (texto oficial del formulario)",
                options=[Option(label=label, value=v) for v, label in enumerate(FREQUENCY_ES)],
            )
            for n in range(1, scale.items + 1)
        ],
        scoring=Scoring(
            bands=[
                Band(min=lo, max=hi, label=label, severity=SEVERITY[label])  # type: ignore[arg-type]
                for lo, hi, (_, label) in zip(lows, highs, scale.bands, strict=True)
            ],
            alerts=alerts,
        ),
        licence=Licence(
            source="Spitzer, Williams, Kroenke et al. (Pfizer)",
            public_domain=True,
            attestation=True,
            note="No permission required to reproduce, translate, display or distribute.",
        ),
    )


TEMPLATES: dict[str, InstrumentSpec] = {
    "phq9": _template("phq9", "PHQ-9 (depresión)"),
    "gad7": _template("gad7", "GAD-7 (ansiedad)"),
}

# --- storage ----------------------------------------------------------------------------

instruments = Table(
    "instruments",
    metadata,
    Column("tenant", String(64), primary_key=True),
    Column("instrument_id", String(40), primary_key=True),
    Column("version", Integer, primary_key=True),
    Column("name", String(120), nullable=False),
    Column("visibility", String(16), nullable=False),  # private | establishment
    Column("status", String(16), nullable=False),  # active | retired
    Column("spec", JSON, nullable=False),
    Column("author_id", String(128), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

instrument_results = Table(
    "instrument_results",
    metadata,
    Column("tenant", String(64), primary_key=True),
    Column("result_id", String(32), primary_key=True),
    Column("patient_id", String(64), nullable=False, index=True),
    Column("instrument_id", String(40), nullable=False),
    Column("version", Integer, nullable=False),
    Column("instrument_name", String(120), nullable=False),
    Column("answers", JSON, nullable=False),
    Column("total", Float, nullable=True),
    Column("subscales", JSON, nullable=False),
    Column("band", String(80), nullable=True),
    Column("severity", String(16), nullable=True),
    Column("alerts", JSON, nullable=False),
    Column("note", String(2000), nullable=True),
    Column("author_id", String(128), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)


def _slug(name: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:24] or "test"
    return f"{base}-{uuid.uuid4().hex[:6]}"


class Instruments:
    def __init__(self, db: Database, audit: AuditLog) -> None:
        self.db = db
        self.audit = audit

    def _visible(self, principal: Principal) -> Any:
        return and_(
            instruments.c.tenant == principal.tenant,
            or_(
                instruments.c.visibility == "establishment",
                instruments.c.author_id == principal.id,
            ),
        )

    @staticmethod
    def _public(row: Any) -> dict[str, Any]:
        return {
            "instrument_id": row.instrument_id,
            "version": row.version,
            "name": row.name,
            "visibility": row.visibility,
            "status": row.status,
            "author_id": row.author_id,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "spec": row.spec,
        }

    async def create(
        self, principal: Principal, spec: InstrumentSpec, visibility: str = "establishment"
    ) -> dict[str, Any]:
        instrument_id = _slug(spec.name)
        await self._write(principal, instrument_id, 1, spec, visibility, "instrument.created")
        return await self.get(principal, instrument_id)

    async def revise(
        self, principal: Principal, instrument_id: str, spec: InstrumentSpec
    ) -> dict[str, Any]:
        """A new version; results already recorded keep the version they were scored with."""
        current = await self.get(principal, instrument_id)
        if current["author_id"] != principal.id and not principal.has(Role.ADMIN):
            raise PermissionError("only the author (or an admin) can revise an instrument")
        await self._write(
            principal,
            instrument_id,
            current["version"] + 1,
            spec,
            current["visibility"],
            "instrument.revised",
        )
        return await self.get(principal, instrument_id)

    async def _write(
        self,
        principal: Principal,
        instrument_id: str,
        version: int,
        spec: InstrumentSpec,
        visibility: str,
        action: str,
    ) -> None:
        if visibility not in ("private", "establishment"):
            raise InstrumentError("visibility is 'private' or 'establishment'")
        async with self.db.engine.begin() as conn:
            await conn.execute(
                insert(instruments).values(
                    tenant=principal.tenant,
                    instrument_id=instrument_id,
                    version=version,
                    name=spec.name,
                    visibility=visibility,
                    status="active",
                    spec=spec.model_dump(mode="json"),
                    author_id=principal.id,
                    created_at=utcnow(),
                )
            )
            await self.audit.record_in(
                conn,
                principal.tenant,
                principal.id,
                action,
                f"instrument/{instrument_id}",
                details={"version": version, "items": len(spec.items)},
            )

    async def available(self, principal: Principal) -> list[dict[str, Any]]:
        """The latest version of every active instrument this professional may use."""
        query = select(instruments).where(self._visible(principal))
        async with self.db.engine.connect() as conn:
            rows = list(await conn.execute(query))
        latest: dict[str, Any] = {}
        for row in rows:
            if row.instrument_id not in latest or row.version > latest[row.instrument_id].version:
                latest[row.instrument_id] = row
        return sorted(
            (self._public(r) for r in latest.values() if r.status == "active"),
            key=lambda r: r["name"].lower(),
        )

    async def get(
        self, principal: Principal, instrument_id: str, version: int | None = None
    ) -> dict[str, Any]:
        query = select(instruments).where(
            and_(self._visible(principal), instruments.c.instrument_id == instrument_id)
        )
        if version is not None:
            query = query.where(instruments.c.version == version)
        query = query.order_by(instruments.c.version.desc()).limit(1)
        async with self.db.engine.connect() as conn:
            row = (await conn.execute(query)).first()
        if row is None:
            raise KeyError(instrument_id)
        return self._public(row)

    async def retire(self, principal: Principal, instrument_id: str) -> None:
        current = await self.get(principal, instrument_id)
        if current["author_id"] != principal.id and not principal.has(Role.ADMIN):
            raise PermissionError("only the author (or an admin) can retire an instrument")
        async with self.db.engine.begin() as conn:
            await conn.execute(
                update(instruments)
                .where(
                    and_(
                        instruments.c.tenant == principal.tenant,
                        instruments.c.instrument_id == instrument_id,
                    )
                )
                .values(status="retired")
            )
            await self.audit.record_in(
                conn,
                principal.tenant,
                principal.id,
                "instrument.retired",
                f"instrument/{instrument_id}",
            )

    async def administer(
        self,
        principal: Principal,
        patient_id: str,
        instrument_id: str,
        answers: dict[str, Any],
        version: int | None = None,
        note: str | None = None,
    ) -> dict[str, Any]:
        await self._require_patient(principal.tenant, patient_id)  # 404 before anything else
        definition = await self.get(principal, instrument_id, version)
        if definition["status"] != "active" and version is None:
            raise InstrumentError("this instrument is retired")
        spec = InstrumentSpec.model_validate(definition["spec"])
        result = score(spec, answers)
        result_id = uuid.uuid4().hex[:16]
        async with self.db.engine.begin() as conn:
            await conn.execute(
                insert(instrument_results).values(
                    tenant=principal.tenant,
                    result_id=result_id,
                    patient_id=patient_id,
                    instrument_id=instrument_id,
                    version=definition["version"],
                    instrument_name=definition["name"],
                    answers=result["answers"],
                    total=result["total"],
                    subscales=result["subscales"],
                    band=result["band"],
                    severity=result["severity"],
                    alerts=result["alerts"],
                    note=note,
                    author_id=principal.id,
                    created_at=utcnow(),
                )
            )
            # The audit names the event, never the answers.
            await self.audit.record_in(
                conn,
                principal.tenant,
                principal.id,
                "instrument.administered",
                f"instrument_result/{result_id}",
                subject_id=patient_id,
                details={
                    "instrument": instrument_id,
                    "version": definition["version"],
                    "alerts": len(result["alerts"]),
                },
            )
        return await self.result(principal, result_id)

    async def _require_patient(self, tenant: str, patient_id: str) -> None:
        query = select(patients.c.id).where(
            and_(patients.c.tenant == tenant, patients.c.id == patient_id)
        )
        async with self.db.engine.connect() as conn:
            if (await conn.execute(query)).first() is None:
                raise KeyError(patient_id)

    @staticmethod
    def _result(row: Any) -> dict[str, Any]:
        return {
            c.name: (
                getattr(row, c.name).isoformat() if c.name == "created_at" else getattr(row, c.name)
            )
            for c in instrument_results.c
            if c.name != "tenant"
        }

    async def result(self, principal: Principal, result_id: str) -> dict[str, Any]:
        query = select(instrument_results).where(
            and_(
                instrument_results.c.tenant == principal.tenant,
                instrument_results.c.result_id == result_id,
            )
        )
        async with self.db.engine.connect() as conn:
            row = (await conn.execute(query)).first()
        if row is None:
            raise KeyError(result_id)
        return self._result(row)

    async def results(self, principal: Principal, patient_id: str) -> list[dict[str, Any]]:
        query = (
            select(instrument_results)
            .where(
                and_(
                    instrument_results.c.tenant == principal.tenant,
                    instrument_results.c.patient_id == patient_id,
                )
            )
            .order_by(instrument_results.c.created_at)
        )
        await self._require_patient(principal.tenant, patient_id)
        async with self.db.engine.connect() as conn:
            rows = list(await conn.execute(query))
        return [self._result(r) for r in rows]

    async def export_subject(self, tenant: str, patient_id: str) -> list[dict[str, Any]]:
        query = select(instrument_results).where(
            and_(
                instrument_results.c.tenant == tenant,
                instrument_results.c.patient_id == patient_id,
            )
        )
        async with self.db.engine.connect() as conn:
            return [self._result(r) for r in await conn.execute(query)]
