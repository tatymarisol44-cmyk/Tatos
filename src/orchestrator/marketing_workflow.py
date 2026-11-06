"""Existing catalog specialists coordinate real, reviewable creative resources.

No patients, contact lists or clinical services are passed to these model calls.
Publication/approval/scheduling remain the existing PublicationService's responsibility.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import uuid
from datetime import date, timedelta
from typing import Any, Literal

from PIL import Image, ImageOps, UnidentifiedImageError
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    Integer,
    LargeBinary,
    String,
    Table,
    insert,
    select,
    update,
)

from orchestrator.catalog import AgentSpec
from orchestrator.creatives import HEX, Brief, Format
from orchestrator.db import Database, metadata, utcnow
from orchestrator.governance import AuditLog, ThreadLeases
from orchestrator.guardrails import check_input, redact_pii
from orchestrator.marketing_designer import (
    DesignBrief,
    DesignError,
    DesignerUnavailable,
    MarketingDesigner,
    MarketingDraft,
)
from orchestrator.publishing import PublicationService

marketing_brands = Table(
    "marketing_brands",
    metadata,
    Column("tenant", String(64), primary_key=True),
    Column("version", Integer, nullable=False),
    Column("spec", JSON, nullable=False),
    Column("logo", LargeBinary, nullable=True),
    Column("logo_sha256", String(64), nullable=True),
)
marketing_workflows = Table(
    "marketing_workflows",
    metadata,
    Column("tenant", String(64), primary_key=True),
    Column("id", String(32), primary_key=True),
    Column("revision", Integer, nullable=False),
    Column("status", String(32), nullable=False),
    Column("payload", JSON, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)


class WorkflowConflict(ValueError):
    pass


class Brand(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=80)
    services: list[str] = Field(default_factory=list, max_length=30)
    background: str = Field(default="#F7F3EE", pattern=HEX)
    accent: str = Field(default="#2F6F73", pattern=HEX)
    text: str = Field(default="#1F2933", pattern=HEX)


class EditorialItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    day: date
    network: Literal["facebook", "instagram", "whatsapp", "app"]
    format: Format = "feed"
    goal: str = Field(min_length=1, max_length=180)


class CampaignPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    strategy: str = Field(min_length=1, max_length=1500)
    audience: str = Field(min_length=1, max_length=300)
    calendar: list[EditorialItem] = Field(min_length=1, max_length=12)


class VisualDirection(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    direction: str = Field(min_length=1, max_length=1000)
    emphasis: Literal["light", "brand"] = "light"


class Optimization(BaseModel):
    recommendations: list[str] = Field(min_length=1, max_length=8)


def _safe_public(value: Any) -> None:
    text = json.dumps(value, ensure_ascii=False)
    if not check_input(text, max_chars=20000).allowed or redact_pii(text)[1]:
        raise DesignError(
            "Usa solo información pública de servicios; no incluyas datos de pacientes."
        )


def _normalize_logo(content: bytes) -> bytes:
    if len(content) > 2_000_000:
        raise DesignError("El logotipo debe pesar menos de 2 MB.")
    try:
        with Image.open(io.BytesIO(content)) as source:
            if (
                source.format not in {"PNG", "JPEG", "WEBP"}
                or source.width * source.height > 9_000_000
            ):
                raise DesignError("Usa un logotipo PNG, JPEG o WebP de hasta 9 megapíxeles.")
            image = ImageOps.exif_transpose(source).convert("RGBA")
            image.thumbnail((600, 300))
            clean = Image.new("RGB", image.size, "white")
            clean.paste(image, mask=image.getchannel("A"))
            out = io.BytesIO()
            clean.save(out, format="JPEG", quality=90)
            return out.getvalue()
    except (OSError, UnidentifiedImageError, Image.DecompressionBombError) as exc:
        raise DesignError("No se pudo leer el logotipo.") from exc


class Branding:
    def __init__(self, db: Database, audit: AuditLog) -> None:
        self.db, self.audit = db, audit

    async def get(self, tenant: str) -> dict[str, Any]:
        async with self.db.engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        select(marketing_brands).where(
                            marketing_brands.c.tenant == tenant,
                        )
                    )
                )
                .mappings()
                .first()
            )
        if row is None:
            return {
                "version": 0,
                "brand": Brand(name="Consultorio").model_dump(),
                "logo_sha256": None,
            }
        return {"version": row["version"], "brand": row["spec"], "logo_sha256": row["logo_sha256"]}

    async def save(self, tenant: str, actor: str, brand: Brand, version: int) -> dict[str, Any]:
        _safe_public(brand.model_dump())
        if any(not s.strip() or len(s) > 200 for s in brand.services):
            raise DesignError("Cada servicio público necesita un nombre de hasta 200 caracteres.")
        async with self.db.engine.begin() as conn:
            if version == 0:
                from sqlalchemy.exc import IntegrityError

                try:
                    await conn.execute(
                        insert(marketing_brands).values(
                            tenant=tenant,
                            version=1,
                            spec=brand.model_dump(),
                        )
                    )
                except IntegrityError as exc:
                    raise WorkflowConflict(
                        "La identidad cambió; recarga antes de guardar."
                    ) from exc
            else:
                result = await conn.execute(
                    update(marketing_brands)
                    .where(
                        marketing_brands.c.tenant == tenant,
                        marketing_brands.c.version == version,
                    )
                    .values(version=version + 1, spec=brand.model_dump())
                )
                if result.rowcount != 1:
                    raise WorkflowConflict("La identidad cambió; recarga antes de guardar.")
            await self.audit.record_in(conn, tenant, actor, "marketing.brand.updated", "brand")
        return await self.get(tenant)

    async def logo(self, tenant: str) -> bytes | None:
        async with self.db.engine.connect() as conn:
            return (
                await conn.execute(
                    select(marketing_brands.c.logo).where(
                        marketing_brands.c.tenant == tenant,
                    )
                )
            ).scalar_one_or_none()

    async def set_logo(
        self, tenant: str, actor: str, content: bytes, version: int
    ) -> dict[str, Any]:
        normalized = await asyncio.to_thread(_normalize_logo, content)
        digest = hashlib.sha256(normalized).hexdigest()
        async with self.db.engine.begin() as conn:
            result = await conn.execute(
                update(marketing_brands)
                .where(
                    marketing_brands.c.tenant == tenant,
                    marketing_brands.c.version == version,
                )
                .values(logo=normalized, logo_sha256=digest, version=version + 1)
            )
            if result.rowcount != 1:
                raise WorkflowConflict("Guarda o recarga la identidad antes de subir el logotipo.")
            await self.audit.record_in(
                conn, tenant, actor, "marketing.brand.logo", "brand", details={"sha256": digest}
            )
        return await self.get(tenant)


class MarketingWorkflow:
    def __init__(
        self,
        db: Database,
        audit: AuditLog,
        designer: MarketingDesigner,
        branding: Branding,
        publications: PublicationService,
        leases: ThreadLeases,
    ) -> None:
        self.db, self.audit = db, audit
        self.designer, self.branding, self.publications = designer, branding, publications
        self.leases = leases

    def design_agent(self) -> AgentSpec:
        for name in ("design-visual-storyteller", "design-brand-guardian", "design-ui-designer"):
            agent = self.designer.catalog.get(name)
            if agent and not agent.remote_url:
                return agent
        raise DesignerUnavailable("No está cargado el agente de diseño existente del catálogo.")

    async def _event(
        self,
        tenant: str,
        actor: str,
        flow: str,
        stage: str,
        agent: str,
        tool: str,
        result: dict[str, Any],
    ) -> None:
        await self.audit.record(
            tenant,
            actor,
            "marketing.workflow." + stage,
            "marketing_workflow/" + flow,
            details={
                "correlation_id": flow,
                "agent_id": agent,
                "tool": tool,
                "catalog_version": self.designer.catalog.version,
                **result,
            },
        )

    async def _structured(
        self, agent: AgentSpec, marker: str, model: type[BaseModel], public: dict[str, Any]
    ) -> BaseModel:
        _safe_public(public)
        rules = (
            "Trabaja solo con los datos públicos aportados. No hay pacientes ni herramientas "
            "de envío disponibles. No inventes precios, testimonios, resultados, servicios "
            "ni métricas. Todo es un borrador pendiente de revisión. Devuelve solo JSON:\n"
            + json.dumps(model.model_json_schema(), ensure_ascii=False)
        )
        reply = await self.designer.llm.complete(
            [
                {"role": "system", "content": agent.system_prompt + "\n" + marker + "\n" + rules},
                {"role": "user", "content": json.dumps(public, ensure_ascii=False)},
            ],
            model=self.designer.settings.llm_model,
            max_tokens=2000,
            temperature=0.3,
        )
        try:
            value = model.model_validate_json(reply.text)
            _safe_public(value.model_dump(mode="json"))
            return value
        except ValidationError as exc:
            raise DesignError("El agente devolvió un resultado incompleto; no se usó.") from exc

    async def get(self, tenant: str, flow: str) -> dict[str, Any]:
        async with self.db.engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        select(marketing_workflows).where(
                            marketing_workflows.c.tenant == tenant,
                            marketing_workflows.c.id == flow,
                        )
                    )
                )
                .mappings()
                .first()
            )
        if row is None:
            raise KeyError(flow)
        result = dict(row)
        result.pop("tenant")
        result["resources"] = []
        for item in result["payload"].get("resources", []):
            resource = await self.publications.get(tenant, item["publication_id"])
            result["resources"].append({**item, "publication": resource})
        return result

    async def list_flows(self, tenant: str) -> list[dict[str, Any]]:
        async with self.db.engine.connect() as conn:
            ids = (
                (
                    await conn.execute(
                        select(marketing_workflows.c.id)
                        .where(
                            marketing_workflows.c.tenant == tenant,
                        )
                        .order_by(marketing_workflows.c.created_at.desc())
                        .limit(100)
                    )
                )
                .scalars()
                .all()
            )
        return [await self.get(tenant, flow) for flow in ids]

    async def create(
        self, tenant: str, actor: str, brief: DesignBrief, first: date
    ) -> dict[str, Any]:
        agent = self.designer._agent()
        visual = self.design_agent()
        brand = await self.branding.get(tenant)
        flow = uuid.uuid4().hex[:16]
        now = utcnow()
        payload: dict[str, Any] = {
            "brief": brief.model_dump(),
            "brand": brand,
            "marketing_agent": agent.id,
            "design_agent": visual.id,
            "simulation": self.designer.settings.llm_backend == "fake",
            "resources": [],
        }
        async with self.db.engine.begin() as conn:
            await conn.execute(
                insert(marketing_workflows).values(
                    tenant=tenant,
                    id=flow,
                    revision=1,
                    status="planning",
                    payload=payload,
                    created_at=now,
                    updated_at=now,
                )
            )
        try:
            await self._event(
                tenant, actor, flow, "started", agent.id, "catalog", {"status": "started"}
            )
            draft = await self.designer.design(tenant, actor, brief)
            payload["draft"] = draft["draft"]
            await self._event(
                tenant,
                actor,
                flow,
                "copy",
                agent.id,
                "MarketingDesigner.design",
                {"status": "draft"},
            )
            public = {
                "brief": brief.model_dump(),
                "brand": brand["brand"],
                "start_date": first.isoformat(),
            }
            plan = await self._structured(agent, "MARKETING WORKFLOW PLAN", CampaignPlan, public)
            validated = CampaignPlan.model_validate(plan)
            if any(
                not first <= item.day <= first + timedelta(days=90) for item in validated.calendar
            ):
                raise DesignError("El calendario debe quedar dentro de los 90 días solicitados.")
            payload["plan"] = validated.model_dump(mode="json")
            await self._event(
                tenant, actor, flow, "plan", agent.id, "LLM.structured", {"status": "draft"}
            )
            direction = await self._structured(
                visual,
                "MARKETING VISUAL DIRECTION",
                VisualDirection,
                {"draft": payload["draft"], "brand": brand["brand"]},
            )
            payload["visual"] = direction.model_dump(mode="json")
            await self._event(
                tenant, actor, flow, "design", visual.id, "LLM.structured", {"status": "draft"}
            )
        except Exception:
            await self._save(tenant, flow, payload, "failed", 1)
            await self._event(
                tenant, actor, flow, "failed", agent.id, "workflow", {"status": "failed"}
            )
            raise
        await self._save(tenant, flow, payload, "awaiting_review", 1)
        return await self.get(tenant, flow)

    async def _save(
        self, tenant: str, flow: str, payload: dict[str, Any], status: str, revision: int
    ) -> None:
        async with self.db.engine.begin() as conn:
            result = await conn.execute(
                update(marketing_workflows)
                .where(
                    marketing_workflows.c.tenant == tenant,
                    marketing_workflows.c.id == flow,
                    marketing_workflows.c.revision == revision,
                )
                .values(payload=payload, status=status, revision=revision + 1, updated_at=utcnow())
            )
            if result.rowcount != 1:
                raise WorkflowConflict("El flujo cambió; recarga antes de continuar.")

    async def edit(
        self,
        tenant: str,
        actor: str,
        flow: str,
        revision: int,
        draft: MarketingDraft,
        plan: CampaignPlan,
    ) -> dict[str, Any]:
        current = await self.get(tenant, flow)
        if current["status"] != "awaiting_review":
            raise WorkflowConflict("Solo se edita el borrador antes de generar sus archivos.")
        _safe_public(draft.model_dump())
        _safe_public(plan.model_dump(mode="json"))
        current["payload"].update(draft=draft.model_dump(), plan=plan.model_dump(mode="json"))
        await self._save(tenant, flow, current["payload"], "awaiting_review", revision)
        await self._event(
            tenant, actor, flow, "edited", "human", "workflow.edit", {"status": "draft"}
        )
        return await self.get(tenant, flow)

    async def render(
        self,
        tenant: str,
        actor: str,
        flow: str,
        revision: int,
        account_id: str,
        formats: list[Format],
    ) -> dict[str, Any]:
        thread, holder = "marketing-flow-" + flow, uuid.uuid4().hex
        await self.leases.acquire(tenant, thread, holder)
        try:
            async with self.leases.keepalive(tenant, thread, holder):
                return await self._render_locked(tenant, actor, flow, revision, account_id, formats)
        finally:
            await self.leases.release(tenant, thread, holder)

    async def _render_locked(
        self,
        tenant: str,
        actor: str,
        flow: str,
        revision: int,
        account_id: str,
        formats: list[Format],
    ) -> dict[str, Any]:
        current = await self.get(tenant, flow)
        if current["status"] not in {"awaiting_review", "render_failed", "rendering"}:
            raise WorkflowConflict("El flujo no está listo para generar diseños.")
        # Refuse another tenant's account before changing state or touching files.
        await self.publications.social.get(tenant, account_id)
        payload = current["payload"]
        if payload.get("render_account") and payload["render_account"] != account_id:
            raise WorkflowConflict("Reanuda con la misma cuenta del diseño inicial.")
        payload["render_account"] = account_id
        await self._save(tenant, flow, payload, "rendering", revision)
        revision += 1
        brand = Brand.model_validate(payload["brand"]["brand"])
        # Bind the logo to the snapshotted identity; do not silently swap it mid-flow.
        logo = await self.branding.logo(tenant)
        if (hashlib.sha256(logo).hexdigest() if logo else None) != payload["brand"]["logo_sha256"]:
            await self._save(tenant, flow, payload, "render_failed", revision)
            raise WorkflowConflict(
                "Cambió el logotipo. Crea un nuevo plan con la identidad actual."
            )
        draft = MarketingDraft.model_validate(payload["draft"])
        emphasis = payload["visual"]["emphasis"] == "brand"
        brief = Brief(
            title=draft.title,
            points=draft.points,
            cta=draft.cta,
            practice_name=brand.name,
            background=brand.accent if emphasis else brand.background,
            accent=brand.background if emphasis else brand.accent,
            text=brand.background if emphasis else brand.text,
        )
        try:
            for fmt in dict.fromkeys(formats):
                if any(item["format"] == fmt for item in payload["resources"]):
                    continue
                resource = await self.publications.create(
                    tenant,
                    actor,
                    account_id=account_id,
                    kind="infographic",
                    brief=brief,
                    caption=draft.caption,
                    fmt=fmt,
                    logo=logo,
                    request_id=f"{flow}:{fmt}",
                )
                payload["resources"].append(
                    {"format": fmt, "publication_id": resource["publication_id"]}
                )
                await self._save(tenant, flow, payload, "rendering", revision)
                revision += 1
                await self._event(
                    tenant,
                    actor,
                    flow,
                    "rendered",
                    payload["design_agent"],
                    "PublicationService.create",
                    {
                        "status": "pending_approval",
                        "publication_id": resource["publication_id"],
                        "sha256": resource["sha256"],
                    },
                )
        except Exception:
            await self._save(tenant, flow, payload, "render_failed", revision)
            await self._event(
                tenant,
                actor,
                flow,
                "render_failed",
                payload["design_agent"],
                "PublicationService.create",
                {"status": "failed"},
            )
            raise
        await self._save(tenant, flow, payload, "ready", revision)
        return await self.get(tenant, flow)

    async def analysis(self, tenant: str, flow: str) -> dict[str, Any]:
        current = await self.get(tenant, flow)
        resources = current["resources"]
        states = [item["publication"]["status"] for item in resources]
        return {
            "correlation_id": flow,
            "source": "local_publication_records",
            "generated": len(resources),
            "published": states.count("published"),
            "pending_approval": states.count("pending_approval"),
            "external_metrics": {
                "status": "blocked",
                "reason": "No se han verificado permisos ni una consulta de métricas oficial.",
            },
            "recommendations": [
                "Revisar y aprobar las creatividades pendientes."
                if "pending_approval" in states
                else "Medir resultados con permisos oficiales "
                "antes de cambiar presupuesto o segmentación."
            ],
            "requires_human_review": True,
        }

    async def optimize(self, tenant: str, actor: str, flow: str, revision: int) -> dict[str, Any]:
        current = await self.get(tenant, flow)
        if current["status"] != "ready":
            raise WorkflowConflict("Genera y revisa los diseños antes de pedir mejoras.")
        measured = await self.analysis(tenant, flow)
        agent = self.designer._agent()
        result = await self._structured(
            agent, "MARKETING WORKFLOW OPTIMIZE", Optimization, {"local_metrics": measured}
        )
        current["payload"]["optimization"] = {
            "proposal": result.model_dump(),
            "source": measured,
            "as_of": utcnow().isoformat(),
            "requires_review": True,
        }
        await self._save(tenant, flow, current["payload"], current["status"], revision)
        await self._event(
            tenant,
            actor,
            flow,
            "optimization",
            agent.id,
            "LLM.structured",
            {"status": "proposal", "source": "local_publication_records"},
        )
        return await self.get(tenant, flow)
