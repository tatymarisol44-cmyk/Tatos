"""Append-only, clinician-entered measurements in a patient's clinical record."""

from __future__ import annotations

import math
import uuid
from datetime import date
from typing import Any

from sqlalchemy import (
    Column,
    Date,
    DateTime,
    Float,
    Index,
    String,
    Table,
    Text,
    and_,
    insert,
    select,
)

from orchestrator.auth import Principal
from orchestrator.crm import patients
from orchestrator.db import Database, aware, metadata, utcnow
from orchestrator.governance import AuditLog

clinical_metrics = Table(
    "clinical_metrics",
    metadata,
    Column("tenant", String(64), primary_key=True),
    Column("metric_id", String(32), primary_key=True),
    Column("patient_id", String(64), nullable=False),
    Column("name", String(120), nullable=False),
    Column("value", Float, nullable=False),
    Column("unit", String(40), nullable=False),
    Column("measured_on", Date, nullable=False),
    Column("note", Text, nullable=True),
    Column("author_id", String(128), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)
Index(
    "ix_clinical_metrics_tenant_patient_date",
    clinical_metrics.c.tenant,
    clinical_metrics.c.patient_id,
    clinical_metrics.c.measured_on,
)


class ClinicalMetricError(ValueError):
    """Invalid measurement or attempt to add data to a restricted patient record."""


def _public(row: Any) -> dict[str, Any]:
    return {
        "metric_id": row.metric_id,
        "patient_id": row.patient_id,
        "name": row.name,
        "value": row.value,
        "unit": row.unit,
        "measured_on": row.measured_on,
        "note": row.note,
        "author_id": row.author_id,
        "created_at": aware(row.created_at),
    }


class ClinicalMetrics:
    def __init__(self, db: Database, audit: AuditLog) -> None:
        self.db = db
        self.audit = audit

    async def record(
        self,
        principal: Principal,
        patient_id: str,
        *,
        name: str,
        value: float,
        unit: str,
        measured_on: date,
        note: str | None,
    ) -> dict[str, Any]:
        name = name.strip()
        unit = unit.strip()
        note = note.strip() or None if note else None
        if not name or not math.isfinite(value):
            raise ClinicalMetricError("a metric needs a name and a finite numeric value")
        if note is not None and not note.strip():
            note = None

        metric_id = uuid.uuid4().hex[:16]
        async with self.db.engine.begin() as conn:
            patient = (
                await conn.execute(
                    select(patients.c.restricted).where(
                        and_(patients.c.tenant == principal.tenant, patients.c.id == patient_id)
                    )
                )
            ).first()
            if patient is None:
                raise KeyError(patient_id)
            if patient.restricted:
                raise ClinicalMetricError("this patient record is restricted")
            values = {
                "tenant": principal.tenant,
                "metric_id": metric_id,
                "patient_id": patient_id,
                "name": name,
                "value": value,
                "unit": unit,
                "measured_on": measured_on,
                "note": note,
                "author_id": principal.id,
                "created_at": utcnow(),
            }
            await conn.execute(insert(clinical_metrics).values(**values))
            await self.audit.record_in(
                conn,
                principal.tenant,
                principal.id,
                "clinical_metric.recorded",
                f"clinical_metric/{metric_id}",
                subject_id=patient_id,
            )
        return await self.get(principal, metric_id)

    async def for_patient(self, principal: Principal, patient_id: str) -> list[dict[str, Any]]:
        exists = select(patients.c.id).where(
            and_(patients.c.tenant == principal.tenant, patients.c.id == patient_id)
        )
        query = (
            select(clinical_metrics)
            .where(
                and_(
                    clinical_metrics.c.tenant == principal.tenant,
                    clinical_metrics.c.patient_id == patient_id,
                )
            )
            .order_by(clinical_metrics.c.measured_on.desc(), clinical_metrics.c.created_at.desc())
        )
        async with self.db.engine.connect() as conn:
            if (await conn.execute(exists)).first() is None:
                raise KeyError(patient_id)
            rows = list(await conn.execute(query))
        await self.audit.record(
            principal.tenant,
            principal.id,
            "clinical_metrics.read",
            f"patient/{patient_id}",
            subject_id=patient_id,
            details={"count": len(rows)},
        )
        return [_public(row) for row in rows]

    async def get(self, principal: Principal, metric_id: str) -> dict[str, Any]:
        query = select(clinical_metrics).where(
            and_(
                clinical_metrics.c.tenant == principal.tenant,
                clinical_metrics.c.metric_id == metric_id,
            )
        )
        async with self.db.engine.connect() as conn:
            row = (await conn.execute(query)).first()
        if row is None:
            raise KeyError(metric_id)
        return _public(row)

    async def export_subject(self, tenant: str, patient_id: str) -> list[dict[str, Any]]:
        query = (
            select(clinical_metrics)
            .where(
                and_(
                    clinical_metrics.c.tenant == tenant,
                    clinical_metrics.c.patient_id == patient_id,
                )
            )
            .order_by(clinical_metrics.c.measured_on, clinical_metrics.c.created_at)
        )
        async with self.db.engine.connect() as conn:
            rows = list(await conn.execute(query))
        return [_public(row) for row in rows]
