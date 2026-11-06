"""Voluntary service feedback, not clinical instruments or diagnostic scores.

Invitations are queued within appointment completion's transaction. App delivery is
real local persistence; social invitations are explicitly blocked, never fabricated.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    Index,
    Integer,
    String,
    Table,
    UniqueConstraint,
    delete,
    func,
    insert,
    select,
    update,
)
from sqlalchemy.exc import IntegrityError

from orchestrator.crm import patients
from orchestrator.db import Database, aware, metadata, utcnow
from orchestrator.governance import AuditLog, Purpose, consents

survey_definitions = Table(
    "survey_definitions",
    metadata,
    Column("tenant", String(64), primary_key=True),
    Column("id", String(32), primary_key=True),
    Column("version", Integer, primary_key=True),
    Column("spec", JSON, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)
survey_settings = Table(
    "survey_settings",
    metadata,
    Column("tenant", String(64), primary_key=True),
    Column("revision", Integer, nullable=False),
    Column("settings", JSON, nullable=False),
)
survey_invitations = Table(
    "survey_invitations",
    metadata,
    Column("tenant", String(64), primary_key=True),
    Column("id", String(32), primary_key=True),
    Column("patient_id", String(64), nullable=False),
    Column("appointment_id", String(64), nullable=False),
    Column("survey_id", String(32), nullable=False),
    Column("version", Integer, nullable=False),
    Column("due_at", DateTime(timezone=True), nullable=False),
    Column("next_reminder_at", DateTime(timezone=True), nullable=False),
    Column("reminder_count", Integer, nullable=False),
    Column("state", String(20), nullable=False),
    Column("answers", JSON, nullable=True),
    Column("satisfaction", Integer, nullable=True),  # normalized 0..100 service rating
    Column("critical", Boolean, nullable=False),
    Column("answered_at", DateTime(timezone=True), nullable=True),
    Column("acknowledged_at", DateTime(timezone=True), nullable=True),
    UniqueConstraint(
        "tenant", "appointment_id", "survey_id", "version", name="uq_survey_invitation"
    ),
    Index("ix_surveys_patient_pending", "tenant", "patient_id", "state", "due_at"),
    Index("ix_surveys_reminders", "state", "next_reminder_at"),
)
survey_events = Table(
    "survey_events",
    metadata,
    Column("tenant", String(64), primary_key=True),
    Column("id", String(32), primary_key=True),
    Column("invitation_id", String(32), nullable=False),
    Column("channel", String(16), nullable=False),
    Column("ordinal", Integer, nullable=False),
    Column("state", String(24), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("tenant", "invitation_id", "channel", "ordinal", name="uq_survey_event"),
    Index("ix_survey_events_tenant_created", "tenant", "created_at"),
)


class SurveyError(ValueError):
    pass


class RatingQuestion(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    id: str = Field(pattern=r"^[\w-]{1,32}$")
    text: str = Field(min_length=1, max_length=250)
    minimum: int = Field(default=1, ge=1, le=9)
    maximum: int = Field(default=5, ge=2, le=10)
    low_label: str = Field(default="Muy insatisfecha", max_length=60)
    high_label: str = Field(default="Muy satisfecha", max_length=60)

    @model_validator(mode="after")
    def bounds(self) -> RatingQuestion:
        if self.minimum >= self.maximum:
            raise ValueError("the maximum must exceed the minimum")
        return self


class SurveySpec(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    title: str = Field(min_length=1, max_length=120)
    questions: list[RatingQuestion] = Field(min_length=1, max_length=20)
    critical_below: int = Field(default=40, ge=0, le=100)

    @model_validator(mode="after")
    def unique_questions(self) -> SurveySpec:
        if len({q.id for q in self.questions}) != len(self.questions):
            raise ValueError("question ids must be unique")
        return self


class SurveySettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    survey_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{1,32}$")
    version: int | None = Field(default=None, ge=1)
    after_minutes: int = Field(default=60, ge=0, le=43200)
    reminder_hours: int = Field(default=48, ge=1, le=720)
    max_reminders: int = Field(default=1, ge=0, le=2)
    channels: list[Literal["app", "whatsapp", "facebook", "instagram"]] = Field(
        default=["app"], min_length=1, max_length=4
    )

    @model_validator(mode="after")
    def active_definition(self) -> SurveySettings:
        if self.enabled and (self.survey_id is None or self.version is None):
            raise ValueError("choose a versioned survey before enabling invitations")
        if len(set(self.channels)) != len(self.channels):
            raise ValueError("channels must not repeat")
        return self


class Surveys:
    def __init__(self, db: Database, audit: AuditLog) -> None:
        self.db, self.audit = db, audit

    async def definitions(self, tenant: str) -> list[dict[str, Any]]:
        async with self.db.engine.connect() as conn:
            rows = (
                (
                    await conn.execute(
                        select(survey_definitions)
                        .where(
                            survey_definitions.c.tenant == tenant,
                        )
                        .order_by(survey_definitions.c.created_at.desc())
                        .limit(100)
                    )
                )
                .mappings()
                .all()
            )
        return [{"id": r["id"], "version": r["version"], "spec": r["spec"]} for r in rows]

    async def definition(self, tenant: str, identifier: str, version: int) -> SurveySpec:
        async with self.db.engine.connect() as conn:
            spec = (
                await conn.execute(
                    select(survey_definitions.c.spec).where(
                        survey_definitions.c.tenant == tenant,
                        survey_definitions.c.id == identifier,
                        survey_definitions.c.version == version,
                    )
                )
            ).scalar_one_or_none()
        if spec is None:
            raise KeyError("survey definition not found")
        return SurveySpec.model_validate(spec)

    async def create(
        self,
        tenant: str,
        actor: str,
        spec: SurveySpec,
        identifier: str | None = None,
        version: int | None = None,
    ) -> dict[str, Any]:
        try:
            return await self._create(tenant, actor, spec, identifier, version)
        except IntegrityError as exc:
            raise SurveyError("the definition changed; reload before revising") from exc

    async def _create(
        self,
        tenant: str,
        actor: str,
        spec: SurveySpec,
        identifier: str | None = None,
        version: int | None = None,
    ) -> dict[str, Any]:
        # Existing versions are immutable. Revising creates the next version under a lock.
        async with self.db.engine.begin() as conn:
            if identifier:
                previous = (
                    (
                        await conn.execute(
                            select(survey_definitions.c.version)
                            .where(
                                survey_definitions.c.tenant == tenant,
                                survey_definitions.c.id == identifier,
                            )
                            .order_by(survey_definitions.c.version.desc())
                            .with_for_update()
                        )
                    )
                    .scalars()
                    .first()
                )
                if previous is None:
                    raise KeyError("survey definition not found")
                if previous != version:
                    raise SurveyError("the definition changed; reload before revising")
                next_version = previous + 1
            else:
                identifier, next_version = uuid.uuid4().hex[:16], 1
            await conn.execute(
                insert(survey_definitions).values(
                    tenant=tenant,
                    id=identifier,
                    version=next_version,
                    spec=spec.model_dump(),
                    created_at=utcnow(),
                )
            )
            await self.audit.record_in(
                conn,
                tenant,
                actor,
                "survey.definition.created",
                f"survey/{identifier}/{next_version}",
            )
        return {"id": identifier, "version": next_version, "spec": spec.model_dump()}

    async def configuration(self, tenant: str) -> dict[str, Any]:
        async with self.db.engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        select(survey_settings).where(
                            survey_settings.c.tenant == tenant,
                        )
                    )
                )
                .mappings()
                .first()
            )
        return {
            "revision": row["revision"] if row else 0,
            "settings": row["settings"] if row else SurveySettings().model_dump(),
            "capabilities": {
                "app": "functional_local",
                "whatsapp": "blocked_external_permission_template_and_adapter",
                "facebook": "blocked_external_permission_window_and_adapter",
                "instagram": "blocked_external_permission_window_and_adapter",
            },
        }

    async def configure(
        self, tenant: str, actor: str, settings: SurveySettings, revision: int
    ) -> dict[str, Any]:
        try:
            return await self._configure(tenant, actor, settings, revision)
        except IntegrityError as exc:
            raise SurveyError("survey settings changed; reload before saving") from exc

    async def _configure(
        self, tenant: str, actor: str, settings: SurveySettings, revision: int
    ) -> dict[str, Any]:
        if settings.enabled:
            assert settings.survey_id and settings.version
            await self.definition(tenant, settings.survey_id, settings.version)
        async with self.db.engine.begin() as conn:
            row = (
                (
                    await conn.execute(
                        select(survey_settings)
                        .where(
                            survey_settings.c.tenant == tenant,
                        )
                        .with_for_update()
                    )
                )
                .mappings()
                .first()
            )
            if (row["revision"] if row else 0) != revision:
                raise SurveyError("survey settings changed; reload before saving")
            if row:
                await conn.execute(
                    update(survey_settings)
                    .where(survey_settings.c.tenant == tenant)
                    .values(settings=settings.model_dump(), revision=revision + 1)
                )
            else:
                await conn.execute(
                    insert(survey_settings).values(
                        tenant=tenant,
                        settings=settings.model_dump(),
                        revision=1,
                    )
                )
            await self.audit.record_in(conn, tenant, actor, "survey.settings.updated", "surveys")
        return await self.configuration(tenant)

    async def _patient(self, conn: Any, tenant: str, patient: str) -> bool:
        active = (
            await conn.execute(
                select(patients.c.id)
                .where(
                    patients.c.tenant == tenant,
                    patients.c.id == patient,
                    patients.c.restricted.is_(False),
                )
                .with_for_update()
            )
        ).first()
        granted = (
            await conn.execute(
                select(consents.c.granted)
                .where(
                    consents.c.tenant == tenant,
                    consents.c.subject_id == patient,
                    consents.c.purpose == Purpose.SURVEYS.value,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        return bool(active and granted)

    async def enqueue_completed(
        self, conn: Any, tenant: str, appointment: str, patient: str, completed_at: datetime
    ) -> None:
        setting = (
            await conn.execute(
                select(survey_settings.c.settings).where(
                    survey_settings.c.tenant == tenant,
                )
            )
        ).scalar_one_or_none()
        config = SurveySettings.model_validate(setting or {})
        if not config.enabled or not await self._patient(conn, tenant, patient):
            return
        assert config.survey_id and config.version
        old = (
            await conn.execute(
                select(survey_invitations.c.id).where(
                    survey_invitations.c.tenant == tenant,
                    survey_invitations.c.appointment_id == appointment,
                    survey_invitations.c.survey_id == config.survey_id,
                    survey_invitations.c.version == config.version,
                )
            )
        ).first()
        if old:
            return
        identifier = uuid.uuid4().hex[:16]
        due = completed_at + timedelta(minutes=config.after_minutes)
        await conn.execute(
            insert(survey_invitations).values(
                tenant=tenant,
                id=identifier,
                patient_id=patient,
                appointment_id=appointment,
                survey_id=config.survey_id,
                version=config.version,
                due_at=due,
                next_reminder_at=due + timedelta(hours=config.reminder_hours),
                reminder_count=0,
                state="pending",
                critical=False,
            )
        )
        for channel in config.channels:
            await conn.execute(
                insert(survey_events).values(
                    tenant=tenant,
                    id=uuid.uuid4().hex[:16],
                    invitation_id=identifier,
                    channel=channel,
                    ordinal=0,
                    state="app_queued" if channel == "app" else "blocked_external",
                    created_at=completed_at,
                )
            )
        await self.audit.record_in(
            conn,
            tenant,
            "system:surveys",
            "survey.invitation.queued",
            f"survey_invitation/{identifier}",
            subject_id=patient,
            details={"appointment_id": appointment, "version": config.version},
        )

    async def pending(self, tenant: str, patient: str) -> dict[str, Any]:
        async with self.db.engine.begin() as conn:
            allowed = await self._patient(conn, tenant, patient)
            if not allowed:
                return {"consent": False, "invitations": []}
            rows = (
                (
                    await conn.execute(
                        select(survey_invitations)
                        .where(
                            survey_invitations.c.tenant == tenant,
                            survey_invitations.c.patient_id == patient,
                            survey_invitations.c.state == "pending",
                            survey_invitations.c.due_at <= utcnow(),
                        )
                        .order_by(survey_invitations.c.due_at)
                    )
                )
                .mappings()
                .all()
            )
        invitations = []
        for row in rows:
            spec = await self.definition(tenant, row["survey_id"], row["version"])
            invitations.append(
                {
                    "id": row["id"],
                    "title": spec.title,
                    "questions": [q.model_dump() for q in spec.questions],
                    "reminder_count": row["reminder_count"],
                }
            )
        return {"consent": True, "invitations": invitations}

    async def answer(
        self, tenant: str, patient: str, identifier: str, answers: dict[str, int], actor: str
    ) -> dict[str, Any]:
        async with self.db.engine.begin() as conn:
            if not await self._patient(conn, tenant, patient):
                raise SurveyError("survey permission or patient record is not available")
            row = (
                (
                    await conn.execute(
                        select(survey_invitations)
                        .where(
                            survey_invitations.c.tenant == tenant,
                            survey_invitations.c.id == identifier,
                            survey_invitations.c.patient_id == patient,
                        )
                        .with_for_update()
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                raise KeyError("survey invitation not found")
            if row["state"] == "answered":
                if row["answers"] != answers:
                    raise SurveyError("the survey was already answered")
                return {"status": "answered", "id": identifier}
            due = aware(row["due_at"])
            assert due is not None
            if row["state"] != "pending" or due > utcnow():
                raise SurveyError("the invitation is not available")
            raw = (
                await conn.execute(
                    select(survey_definitions.c.spec).where(
                        survey_definitions.c.tenant == tenant,
                        survey_definitions.c.id == row["survey_id"],
                        survey_definitions.c.version == row["version"],
                    )
                )
            ).scalar_one()
            spec = SurveySpec.model_validate(raw)
            if set(answers) != {q.id for q in spec.questions} or any(
                type(answers[q.id]) is not int or not q.minimum <= answers[q.id] <= q.maximum
                for q in spec.questions
            ):
                raise SurveyError("answer every service question within its rating scale")
            mean = round(
                100
                * sum((answers[q.id] - q.minimum) / (q.maximum - q.minimum) for q in spec.questions)
                / len(spec.questions)
            )
            await conn.execute(
                update(survey_invitations)
                .where(
                    survey_invitations.c.tenant == tenant,
                    survey_invitations.c.id == identifier,
                )
                .values(
                    state="answered",
                    answers=answers,
                    satisfaction=mean,
                    critical=mean < spec.critical_below,
                    answered_at=utcnow(),
                )
            )
            await self.audit.record_in(
                conn,
                tenant,
                actor,
                "survey.answered",
                f"survey_invitation/{identifier}",
                subject_id=patient,
            )
        return {"status": "answered", "id": identifier}

    async def tick(self, tenant: str | None = None, now: datetime | None = None) -> dict[str, int]:
        now = now or utcnow()
        where = [
            survey_invitations.c.state == "pending",
            survey_invitations.c.next_reminder_at <= now,
            survey_settings.c.settings["enabled"].as_boolean().is_(True),
            survey_invitations.c.reminder_count
            < survey_settings.c.settings["max_reminders"].as_integer(),
            patients.c.restricted.is_(False),
            consents.c.granted.is_(True),
        ]
        if tenant:
            where.append(survey_invitations.c.tenant == tenant)
        async with self.db.engine.connect() as conn:
            targets = (
                await conn.execute(
                    select(
                        survey_invitations.c.tenant,
                        survey_invitations.c.id,
                        survey_invitations.c.patient_id,
                    )
                    .select_from(
                        survey_invitations.join(
                            survey_settings,
                            survey_settings.c.tenant == survey_invitations.c.tenant,
                        )
                        .join(
                            patients,
                            (patients.c.tenant == survey_invitations.c.tenant)
                            & (patients.c.id == survey_invitations.c.patient_id),
                        )
                        .join(
                            consents,
                            (consents.c.tenant == survey_invitations.c.tenant)
                            & (consents.c.subject_id == survey_invitations.c.patient_id)
                            & (consents.c.purpose == Purpose.SURVEYS.value),
                        )
                    )
                    .where(*where)
                    .order_by(survey_invitations.c.next_reminder_at, survey_invitations.c.id)
                    .limit(100)
                )
            ).all()
        reminded, blocked_external = 0, 0
        for owner, identifier, patient in targets:
            async with self.db.engine.begin() as conn:
                if not await self._patient(conn, owner, patient):
                    continue
                row = (
                    (
                        await conn.execute(
                            select(survey_invitations)
                            .where(
                                survey_invitations.c.tenant == owner,
                                survey_invitations.c.id == identifier,
                            )
                            .with_for_update()
                        )
                    )
                    .mappings()
                    .first()
                )
                if row is None:
                    continue
                settings = (
                    await conn.execute(
                        select(survey_settings.c.settings).where(
                            survey_settings.c.tenant == owner,
                        )
                    )
                ).scalar_one_or_none()
                config = SurveySettings.model_validate(settings or {})
                next_at = aware(row["next_reminder_at"])
                assert next_at is not None
                if row["state"] != "pending" or not config.enabled or next_at > now:
                    continue
                if row["reminder_count"] >= config.max_reminders:
                    continue
                ordinal = row["reminder_count"] + 1
                await conn.execute(
                    update(survey_invitations)
                    .where(
                        survey_invitations.c.tenant == owner,
                        survey_invitations.c.id == identifier,
                    )
                    .values(
                        reminder_count=ordinal,
                        next_reminder_at=now + timedelta(hours=config.reminder_hours),
                    )
                )
                for channel in config.channels:
                    await conn.execute(
                        insert(survey_events).values(
                            tenant=owner,
                            id=uuid.uuid4().hex[:16],
                            invitation_id=identifier,
                            channel=channel,
                            ordinal=ordinal,
                            state="app_reminder" if channel == "app" else "blocked_external",
                            created_at=now,
                        )
                    )
                await self.audit.record_in(
                    conn,
                    owner,
                    "system:surveys",
                    "survey.reminder.available",
                    f"survey_invitation/{identifier}",
                    subject_id=row["patient_id"],
                )
                reminded += int("app" in config.channels)
                blocked_external += len([c for c in config.channels if c != "app"])
        return {"app_reminders": reminded, "external_blocked": blocked_external, "external_sent": 0}

    async def report(
        self, tenant: str, start: datetime | None = None, end: datetime | None = None
    ) -> dict[str, Any]:
        where = [survey_invitations.c.tenant == tenant]
        if start is not None:
            where.append(survey_invitations.c.due_at >= aware(start))
        if end is not None:
            where.append(survey_invitations.c.due_at < aware(end))
        breakdown: dict[tuple[str, int], dict[str, tuple[int, int]]] = {}
        async with self.db.engine.connect() as conn:
            aggregates = (
                (
                    await conn.execute(
                        select(
                            survey_invitations.c.survey_id,
                            survey_invitations.c.version,
                            func.count().label("invited"),
                            func.count(survey_invitations.c.answered_at).label("answered"),
                            func.avg(survey_invitations.c.satisfaction).label("mean"),
                        )
                        .where(*where)
                        .group_by(
                            survey_invitations.c.survey_id,
                            survey_invitations.c.version,
                        )
                    )
                )
                .mappings()
                .all()
            )
            stream = await conn.stream(
                select(
                    survey_invitations.c.survey_id,
                    survey_invitations.c.version,
                    survey_invitations.c.answers,
                ).where(*where, survey_invitations.c.answers.is_not(None))
            )
            async for identifier, version, answers in stream:
                group = breakdown.setdefault((identifier, version), {})
                for question, value in answers.items():
                    if type(value) is int:
                        total, count = group.get(question, (0, 0))
                        group[question] = (total + value, count + 1)
            alerts = (
                (
                    await conn.execute(
                        select(
                            survey_invitations.c.id,
                            survey_invitations.c.answered_at,
                            survey_invitations.c.satisfaction,
                            survey_invitations.c.patient_id,
                        ).where(
                            survey_invitations.c.tenant == tenant,
                            survey_invitations.c.critical.is_(True),
                            survey_invitations.c.acknowledged_at.is_(None),
                        )
                    )
                )
                .mappings()
                .all()
            )
        return {
            "groups": [
                {
                    **dict(r),
                    "question_means": [
                        {"question_id": question, "mean_rating": total / count, "responses": count}
                        for question, (total, count) in breakdown.get(
                            (r["survey_id"], r["version"]), {}
                        ).items()
                    ],
                }
                for r in aggregates
            ],
            "critical_alerts": [
                {**dict(r), "answered_at": aware(r["answered_at"])} for r in alerts
            ],
            "period": {"from": start, "to": end},
            "meaning": "Service satisfaction only; not a clinical or validated psychometric score.",
        }

    async def events(self, tenant: str) -> list[dict[str, Any]]:
        async with self.db.engine.connect() as conn:
            rows = (
                (
                    await conn.execute(
                        select(survey_events)
                        .where(
                            survey_events.c.tenant == tenant,
                        )
                        .order_by(survey_events.c.created_at.desc())
                        .limit(200)
                    )
                )
                .mappings()
                .all()
            )
        return [
            {
                "id": r["id"],
                "invitation_id": r["invitation_id"],
                "channel": r["channel"],
                "ordinal": r["ordinal"],
                "state": r["state"],
                "created_at": aware(r["created_at"]),
            }
            for r in rows
        ]

    async def acknowledge(self, tenant: str, identifier: str, actor: str) -> dict[str, bool]:
        async with self.db.engine.begin() as conn:
            result = await conn.execute(
                update(survey_invitations)
                .where(
                    survey_invitations.c.tenant == tenant,
                    survey_invitations.c.id == identifier,
                    survey_invitations.c.critical.is_(True),
                )
                .values(acknowledged_at=utcnow())
            )
            if result.rowcount != 1:
                raise KeyError("critical service response not found")
            await self.audit.record_in(
                conn, tenant, actor, "survey.critical.reviewed", f"survey_invitation/{identifier}"
            )
        return {"reviewed": True}

    async def export_subject(self, tenant: str, patient: str) -> list[dict[str, Any]]:
        async with self.db.engine.connect() as conn:
            rows = (
                (
                    await conn.execute(
                        select(survey_invitations).where(
                            survey_invitations.c.tenant == tenant,
                            survey_invitations.c.patient_id == patient,
                        )
                    )
                )
                .mappings()
                .all()
            )
            own_events = (
                (
                    await conn.execute(
                        select(survey_events)
                        .join(
                            survey_invitations,
                            (
                                (survey_invitations.c.tenant == survey_events.c.tenant)
                                & (survey_invitations.c.id == survey_events.c.invitation_id)
                            ),
                        )
                        .where(
                            survey_invitations.c.tenant == tenant,
                            survey_invitations.c.patient_id == patient,
                        )
                    )
                )
                .mappings()
                .all()
            )
        exported = []
        for row in rows:
            definition = await self.definition(tenant, row["survey_id"], row["version"])
            exported.append(
                {
                    **{
                        k: row[k] for k in ("id", "survey_id", "version", "answers", "satisfaction")
                    },
                    "answered_at": aware(row["answered_at"]),
                    "definition": definition.model_dump(),
                    "events": [
                        {
                            "channel": e["channel"],
                            "state": e["state"],
                            "created_at": aware(e["created_at"]),
                        }
                        for e in own_events
                        if e["invitation_id"] == row["id"]
                    ],
                }
            )
        return exported

    async def erase_subject(self, tenant: str, patient: str) -> int:
        async with self.db.engine.begin() as conn:
            owned = select(survey_invitations.c.id).where(
                survey_invitations.c.tenant == tenant,
                survey_invitations.c.patient_id == patient,
            )
            await conn.execute(
                delete(survey_events).where(
                    survey_events.c.tenant == tenant, survey_events.c.invitation_id.in_(owned)
                )
            )
            result = await conn.execute(
                delete(survey_invitations).where(
                    survey_invitations.c.tenant == tenant,
                    survey_invitations.c.patient_id == patient,
                )
            )
        return int(result.rowcount)
