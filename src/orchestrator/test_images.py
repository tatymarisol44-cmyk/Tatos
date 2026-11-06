"""Clinician-owned test images and reviewed visual report drafts, isolated by tenant."""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from typing import Any

from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    Integer,
    String,
    Table,
    Text,
    and_,
    delete,
    func,
    insert,
    select,
    update,
)

from orchestrator import test_scoring
from orchestrator.auth import Principal
from orchestrator.clinical_files import (
    ClinicalFiles,
    FileRejected,
    clinical_files,
    safe_name,
    sniff,
)
from orchestrator.config import Settings
from orchestrator.crm import patients
from orchestrator.db import Database, metadata, utcnow
from orchestrator.governance import AuditLog
from orchestrator.instruments import Instruments
from orchestrator.test_parameters import ApplicationData, TestParameters, context_text, ready
from orchestrator.test_vision import ImageKind, PrivateVision, draft_text, prepare_image

test_image_reports = Table(
    "test_image_reports",
    metadata,
    Column("tenant", String(64), primary_key=True),
    Column("report_id", String(32), primary_key=True),
    Column("patient_id", String(64), nullable=False, index=True),
    Column("file_id", String(32), nullable=False),
    Column("author_id", String(128), nullable=False),
    Column("title", String(120), nullable=False),
    Column("kind", String(16), nullable=False),
    Column("status", String(16), nullable=False),
    Column("revision", Integer, nullable=False),
    Column("body", Text, nullable=False),
    Column("parameters", JSON, nullable=True),
    Column("application", JSON, nullable=True),
    Column("score_data", JSON, nullable=True),
    Column("extraction", JSON, nullable=True),
    Column("model", String(128), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("reviewed_at", DateTime(timezone=True), nullable=True),
)


class ImageReportConflict(ValueError):
    pass


class ImageReportRestricted(ValueError):
    pass


class TestImages:
    def __init__(
        self,
        db: Database,
        audit: AuditLog,
        files: ClinicalFiles,
        settings: Settings,
        instruments: Instruments,
    ) -> None:
        self.db, self.audit, self.files = db, audit, files
        self.settings = settings
        self.instruments = instruments
        self.vision = PrivateVision(settings)

    @staticmethod
    def _visible(p: Principal) -> Any:
        if not p.is_clinician:
            raise PermissionError("clinical role required")
        return and_(test_image_reports.c.tenant == p.tenant, test_image_reports.c.author_id == p.id)

    async def _patient(self, p: Principal, patient_id: str, *, writing: bool = False) -> None:
        self._visible(p)
        async with self.db.engine.connect() as conn:
            row = (
                await conn.execute(
                    select(patients.c.restricted).where(
                        and_(
                            patients.c.tenant == p.tenant,
                            patients.c.id == patient_id,
                        )
                    )
                )
            ).first()
        if row is None:
            raise KeyError(patient_id)
        if writing and row.restricted:
            raise ImageReportRestricted(
                "La ficha está restringida; no admite nuevos análisis ni cambios."
            )

    async def upload(
        self,
        p: Principal,
        patient_id: str,
        *,
        filename: str,
        content: bytes,
        title: str,
        kind: ImageKind,
        parameters: TestParameters | None = None,
        application: ApplicationData | None = None,
        instrument_id: str = "",
        instrument_version: int | None = None,
    ) -> dict[str, Any]:
        await self._patient(p, patient_id, writing=True)
        score_data = await self._binding(p, instrument_id, instrument_version, kind)
        if not content or len(content) > self.settings.clinical_file_max_bytes:
            raise FileRejected("La imagen está vacía o supera el tamaño permitido.")
        if sniff(content) not in ("image/png", "image/jpeg"):
            raise FileRejected("Elige una imagen PNG o JPEG.")
        await asyncio.to_thread(prepare_image, content)  # decode before storing anything
        file_id = uuid.uuid4().hex[:16]
        report_id = uuid.uuid4().hex
        async with self.db.engine.begin() as conn:
            current = (
                await conn.execute(
                    select(patients.c.restricted)
                    .where(
                        and_(
                            patients.c.tenant == p.tenant,
                            patients.c.id == patient_id,
                        )
                    )
                    .with_for_update()
                )
            ).first()
            if current is None:
                raise KeyError(patient_id)
            if current.restricted:
                raise ImageReportRestricted("La ficha está restringida.")
            await conn.execute(
                insert(clinical_files).values(
                    tenant=p.tenant,
                    file_id=file_id,
                    patient_id=patient_id,
                    label=title,
                    filename=safe_name(filename),
                    content=content,
                    media_type=sniff(content),
                    size=len(content),
                    sha256=hashlib.sha256(content).hexdigest(),
                    access="author_only",
                    author_id=p.id,
                    created_at=utcnow(),
                )
            )
            await conn.execute(
                insert(test_image_reports).values(
                    tenant=p.tenant,
                    report_id=report_id,
                    patient_id=patient_id,
                    file_id=file_id,
                    author_id=p.id,
                    title=title,
                    kind=kind,
                    status="uploaded",
                    revision=1,
                    body="",
                    parameters=(parameters or TestParameters()).model_dump(mode="json"),
                    application=(application or ApplicationData()).model_dump(mode="json"),
                    score_data=score_data,
                    created_at=utcnow(),
                )
            )
            await self.audit.record_in(
                conn,
                p.tenant,
                p.id,
                "test_image.uploaded",
                f"test_image/{report_id}",
                subject_id=patient_id,
            )
        return await self.get(p, report_id)

    async def get(self, p: Principal, report_id: str) -> dict[str, Any]:
        async with self.db.engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        select(test_image_reports).where(
                            and_(
                                self._visible(p),
                                test_image_reports.c.report_id == report_id,
                            )
                        )
                    )
                )
                .mappings()
                .first()
            )
        if row is None:
            raise KeyError(report_id)
        result = dict(row)
        result.pop("tenant")
        source = await self.files.info(p, str(row["file_id"]))
        result["source_sha256"] = source["sha256"]
        result["filename"] = source["filename"]
        parameters = TestParameters.model_validate(result["parameters"] or {})
        application = ApplicationData.model_validate(result["application"] or {})
        result["parameters"] = parameters.model_dump(mode="json")
        result["application"] = application.model_dump(mode="json")
        result["parameters_ready"] = ready(parameters, application)
        result["parameter_summary"] = context_text(parameters, application)
        result["scoring_summary"] = test_scoring.summary(result["score_data"])
        await self.audit.record(
            p.tenant,
            p.id,
            "test_image.read",
            f"test_image/{report_id}",
            subject_id=str(row["patient_id"]),
        )
        return result

    async def for_patient(self, p: Principal, patient_id: str) -> list[dict[str, Any]]:
        await self._patient(p, patient_id)
        async with self.db.engine.connect() as conn:
            rows = (
                (
                    await conn.execute(
                        select(
                            test_image_reports.c.report_id,
                            test_image_reports.c.title,
                            test_image_reports.c.kind,
                            test_image_reports.c.status,
                            test_image_reports.c.created_at,
                        )
                        .where(
                            and_(self._visible(p), test_image_reports.c.patient_id == patient_id)
                        )
                        .order_by(test_image_reports.c.created_at.desc())
                    )
                )
                .mappings()
                .all()
            )
        await self.audit.record(
            p.tenant, p.id, "test_image.list", "test_images", subject_id=patient_id
        )
        return [dict(row) for row in rows]

    async def analyze(self, p: Principal, report_id: str, revision: int) -> dict[str, Any]:
        row = await self.get(p, report_id)
        await self._patient(p, row["patient_id"], writing=True)
        self._editable(row, revision)
        self._require_parameters(row)
        _, content = await self.files.download(p, row["file_id"])
        prepared = await asyncio.to_thread(prepare_image, content)
        score_data = row["score_data"]
        if score_data:
            items = [
                {
                    "item_id": item["id"],
                    "text": item["text"],
                    "options": item.get("options", []),
                    "type": item["type"],
                    "min": item.get("min"),
                    "max": item.get("max"),
                }
                for item in score_data["spec"]["items"]
            ]
            extraction = await self.vision.extract(prepared, row["kind"], items)
            score_data = test_scoring.proposals(score_data, extraction.answers)
        else:
            extraction = await self.vision.extract(prepared, row["kind"])
        await self._update(
            p,
            row,
            revision,
            {
                "extraction": extraction.model_dump(),
                "body": draft_text(extraction, row["kind"]),
                "model": self.settings.clinical_vision_model,
                "status": "draft",
                "score_data": score_data,
            },
            "test_image.analyzed",
        )
        return await self.get(p, report_id)

    @staticmethod
    def _editable(row: dict[str, Any], revision: int) -> None:
        if row["status"] == "reviewed":
            raise ImageReportConflict(
                "El informe revisado se conserva sin cambios. Crea otro informe."
            )
        if row["revision"] != revision:
            raise ImageReportConflict("El informe cambió mientras lo editabas. Vuelve a abrirlo.")

    async def save(
        self,
        p: Principal,
        report_id: str,
        *,
        revision: int,
        body: str | None = None,
        reviewed: bool = False,
        parameters: dict[str, Any] | None = None,
        application: dict[str, Any] | None = None,
        instrument_id: str | None = None,
        instrument_version: int | None = None,
        clear_scoring: bool = False,
    ) -> dict[str, Any]:
        row = await self.get(p, report_id)
        await self._patient(p, row["patient_id"], writing=True)
        self._editable(row, revision)
        new_body = row["body"] if body is None else body.strip()
        new_parameters = TestParameters.model_validate(
            {**row["parameters"], **parameters} if parameters is not None else row["parameters"]
        )
        new_application = ApplicationData.model_validate(
            {**row["application"], **application} if application is not None else row["application"]
        )
        if clear_scoring and instrument_id:
            raise FileRejected("Elige un test o quita la asociación, no ambas opciones.")
        score_data = row["score_data"]
        if clear_scoring:
            score_data = None
        elif instrument_id and (
            not score_data
            or (score_data["instrument_id"], score_data["version"])
            != (
                instrument_id,
                instrument_version,
            )
        ):
            score_data = await self._binding(p, instrument_id, instrument_version, row["kind"])
        if score_data and (
            new_parameters.model_dump(mode="json") != row["parameters"]
            or new_application.model_dump(mode="json") != row["application"]
        ):
            score_data = {**score_data, "verified": False, "result": None}
        if not new_body and (
            reviewed
            or (
                parameters is None
                and application is None
                and instrument_id is None
                and not clear_scoring
            )
        ):
            raise FileRejected("Escribe el contenido del informe.")
        if reviewed and not ready(new_parameters, new_application):
            raise FileRejected(
                "Completa versión, criterios, fuente y fecha de aplicación antes de revisar."
            )
        if reviewed and score_data and not score_data.get("verified"):
            raise FileRejected(
                "Comprueba las respuestas y calcula la banda antes de revisar el informe."
            )
        await self._update(
            p,
            row,
            revision,
            {
                "body": new_body,
                "parameters": new_parameters.model_dump(mode="json"),
                "application": new_application.model_dump(mode="json"),
                "status": "reviewed" if reviewed else ("draft" if new_body else "uploaded"),
                "reviewed_at": utcnow() if reviewed else None,
                "score_data": score_data,
            },
            "test_image.reviewed" if reviewed else "test_image.draft_saved",
        )
        return await self.get(p, report_id)

    async def _binding(
        self, p: Principal, instrument_id: str, version: int | None, kind: str
    ) -> dict[str, Any] | None:
        if not instrument_id:
            return None
        if version is None:
            raise FileRejected("Elige una versión del test para conservar su baremo.")
        definition = await self.instruments.get(p, instrument_id, version)
        return test_scoring.snapshot(definition, kind)

    async def score_answers(
        self,
        p: Principal,
        report_id: str,
        *,
        revision: int,
        answers: dict[str, Any],
        confirmed: bool,
    ) -> dict[str, Any]:
        row = await self.get(p, report_id)
        await self._patient(p, row["patient_id"], writing=True)
        self._editable(row, revision)
        self._require_parameters(row)
        if not confirmed:
            raise FileRejected("Confirma que comparaste todas las respuestas con la imagen.")
        if not row["score_data"]:
            raise FileRejected("Asocia un test con puntuación y bandas antes de calcular.")
        scored = test_scoring.verified_score(row["score_data"], answers)
        await self._update(p, row, revision, {"score_data": scored}, "test_image.scored")
        return await self.get(p, report_id)

    @staticmethod
    def _require_parameters(row: dict[str, Any]) -> None:
        if not row["parameters_ready"]:
            raise FileRejected(
                "Completa versión, criterios, fuente y fecha de aplicación antes de generar."
            )

    async def _update(
        self, p: Principal, row: dict[str, Any], revision: int, values: dict[str, Any], event: str
    ) -> None:
        async with self.db.engine.begin() as conn:
            # Restriction can change while the model is working; recheck at commit.
            active_patient = (
                select(patients.c.id)
                .where(
                    and_(
                        patients.c.tenant == p.tenant,
                        patients.c.id == row["patient_id"],
                        patients.c.restricted.is_(False),
                    )
                )
                .exists()
            )
            changed = await conn.execute(
                update(test_image_reports)
                .where(
                    and_(
                        self._visible(p),
                        test_image_reports.c.report_id == row["report_id"],
                        test_image_reports.c.revision == revision,
                        active_patient,
                    )
                )
                .values(**values, revision=revision + 1)
            )
            if changed.rowcount != 1:
                raise ImageReportConflict("El informe o la ficha cambiaron. Vuelve a abrirlos.")
            await self.audit.record_in(
                conn,
                p.tenant,
                p.id,
                event,
                f"test_image/{row['report_id']}",
                subject_id=row["patient_id"],
                details={"revision": revision + 1},
            )

    async def remove(self, p: Principal, report_id: str, revision: int) -> None:
        row = await self.get(p, report_id)
        await self._patient(p, row["patient_id"], writing=True)
        self._editable(row, revision)
        async with self.db.engine.begin() as conn:
            removed = await conn.execute(
                delete(test_image_reports).where(
                    and_(
                        self._visible(p),
                        test_image_reports.c.report_id == report_id,
                        test_image_reports.c.revision == revision,
                    )
                )
            )
            if removed.rowcount != 1:
                raise ImageReportConflict("El informe cambió. Vuelve a abrirlo.")
            await conn.execute(
                delete(clinical_files).where(
                    and_(
                        clinical_files.c.tenant == p.tenant,
                        clinical_files.c.file_id == row["file_id"],
                        clinical_files.c.author_id == p.id,
                    )
                )
            )
            await self.audit.record_in(
                conn,
                p.tenant,
                p.id,
                "test_image.draft_deleted",
                f"test_image/{report_id}",
                subject_id=row["patient_id"],
            )

    async def export_subject(self, tenant: str, patient_id: str) -> dict[str, Any]:
        # Same author-only treatment as psychotherapy notes, including their drafts.
        async with self.db.engine.connect() as conn:
            count = await conn.scalar(
                select(func.count())
                .select_from(test_image_reports)
                .where(
                    and_(
                        test_image_reports.c.tenant == tenant,
                        test_image_reports.c.patient_id == patient_id,
                    )
                )
            )
        return {"reports": [], "restricted_reports_withheld": count}
