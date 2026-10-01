"""Governance records (the ERM data layer): audit trail, human-review queue, consents.

- Audit trail: append-only events (who did what to which data subject). Kept apart from
  conversations, which are purged after THREAD_RETENTION_DAYS: HIPAA asks for audit
  records to be kept for six years, and erasure requests must stay provable.
- Review queue: answers paused by the graph for a human decision (interrupt + checkpoint).
- Consents: one current state per (tenant, subject, purpose); every change is also an
  audit event, which is the proof of consent GDPR Art. 7(1) asks for.

Subjects are identified by a pseudonymous `subject_id` chosen by the tenant (a patient or
customer number), never by name or e-mail."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    Integer,
    String,
    Table,
    and_,
    delete,
    func,
    insert,
    select,
    update,
)
from sqlalchemy.ext.asyncio import AsyncConnection

from orchestrator.db import Database, aware, metadata, utcnow


class Purpose(StrEnum):
    """What a subject consented to. Treatment itself rests on a different legal basis
    (GDPR Art. 9(2)(h), HIPAA treatment/operations); it is recorded for completeness."""

    TREATMENT = "treatment"
    MARKETING = "marketing"  # campaigns, reminders beyond the appointment itself
    MEMORY = "memory"  # long-term semantic memory of preferences
    PHOTOS = "photos"  # before/after images in marketing
    ANALYTICS = "analytics"  # inclusion in aggregated insights


audit_events = Table(
    "audit_events",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("ts", DateTime(timezone=True), nullable=False, index=True),
    Column("tenant", String(64), nullable=False, index=True),
    Column("actor", String(128), nullable=False),
    Column("action", String(64), nullable=False),
    Column("resource", String(256), nullable=False),
    Column("subject_id", String(64), nullable=True, index=True),
    Column("details", JSON, nullable=False, default=dict),
)

reviews = Table(
    "reviews",
    metadata,
    Column("tenant", String(64), primary_key=True),
    Column("thread_id", String(128), primary_key=True),
    Column("status", String(16), nullable=False, index=True),  # pending|approved|rejected
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("resolved_at", DateTime(timezone=True), nullable=True),
    Column("subject_id", String(64), nullable=True),
    Column("payload", JSON, nullable=False),
    Column("decision", JSON, nullable=True),
)

consents = Table(
    "consents",
    metadata,
    Column("tenant", String(64), primary_key=True),
    Column("subject_id", String(64), primary_key=True),
    Column("purpose", String(32), primary_key=True),
    Column("granted", Boolean, nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    Column("source", String(128), nullable=False),
)


@dataclass(frozen=True)
class AuditEvent:
    id: int
    ts: datetime
    tenant: str
    actor: str
    action: str
    resource: str
    subject_id: str | None
    details: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {**self.__dict__, "ts": self.ts.isoformat()}


class AuditLog:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def record(
        self,
        tenant: str,
        actor: str,
        action: str,
        resource: str,
        *,
        subject_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        """Audit an action that has no write of its own (reads, exports)."""
        async with self.db.engine.begin() as conn:
            await self.record_in(
                conn, tenant, actor, action, resource, subject_id=subject_id, details=details
            )

    async def record_in(
        self,
        conn: AsyncConnection,
        tenant: str,
        actor: str,
        action: str,
        resource: str,
        *,
        subject_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        """Audit inside the caller's transaction: the business write and its event
        commit or roll back together (no change without its audit event)."""
        await conn.execute(
            insert(audit_events).values(
                ts=utcnow(),
                tenant=tenant,
                actor=actor,
                action=action,
                resource=resource,
                subject_id=subject_id,
                details=details or {},
            )
        )

    async def list(
        self, tenant: str, *, subject_id: str | None = None, limit: int | None = 100
    ) -> list[AuditEvent]:
        """Newest first. `limit=None` returns every event (subject exports)."""
        query = select(audit_events).where(audit_events.c.tenant == tenant)
        if subject_id is not None:
            query = query.where(audit_events.c.subject_id == subject_id)
        query = query.order_by(audit_events.c.id.desc())
        if limit is not None:
            query = query.limit(limit)
        async with self.db.engine.connect() as conn:
            rows = (await conn.execute(query)).mappings().all()
        return [
            AuditEvent(**{**row, "ts": aware(row["ts"]), "details": row["details"] or {}})
            for row in rows
        ]


@dataclass(frozen=True)
class Review:
    tenant: str
    thread_id: str
    status: str
    created_at: datetime
    resolved_at: datetime | None
    subject_id: str | None
    payload: dict[str, Any]
    decision: dict[str, Any] | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "thread_id": self.thread_id,
            "status": self.status,
            "created_at": self.created_at.isoformat(),
            "resolved_at": self.resolved_at.isoformat() if self.resolved_at else None,
            "subject_id": self.subject_id,
            "payload": self.payload,
            "decision": self.decision,
        }


class ReviewQueue:
    def __init__(self, db: Database) -> None:
        self.db = db

    @staticmethod
    def _row(row: Any) -> Review:
        return Review(
            tenant=row["tenant"],
            thread_id=row["thread_id"],
            status=row["status"],
            created_at=aware(row["created_at"]) or utcnow(),
            resolved_at=aware(row["resolved_at"]),
            subject_id=row["subject_id"],
            payload=row["payload"],
            decision=row["decision"],
        )

    async def open(
        self, tenant: str, thread_id: str, payload: dict[str, Any], subject_id: str | None
    ) -> None:
        """Register (or re-open, for a later turn of the same thread) a pending review."""
        key = and_(reviews.c.tenant == tenant, reviews.c.thread_id == thread_id)
        values = {
            "status": "pending",
            "created_at": utcnow(),
            "resolved_at": None,
            "subject_id": subject_id,
            "payload": payload,
            "decision": None,
        }
        async with self.db.engine.begin() as conn:
            if (await conn.execute(update(reviews).where(key).values(**values))).rowcount == 0:
                await conn.execute(
                    insert(reviews).values(tenant=tenant, thread_id=thread_id, **values)
                )

    async def get(self, tenant: str, thread_id: str) -> Review | None:
        query = select(reviews).where(
            and_(reviews.c.tenant == tenant, reviews.c.thread_id == thread_id)
        )
        async with self.db.engine.connect() as conn:
            row = (await conn.execute(query)).mappings().first()
        return self._row(row) if row else None

    async def list(self, tenant: str, status: str | None = "pending") -> list[Review]:
        query = select(reviews).where(reviews.c.tenant == tenant)
        if status is not None:
            query = query.where(reviews.c.status == status)
        async with self.db.engine.connect() as conn:
            rows = (await conn.execute(query.order_by(reviews.c.created_at))).mappings().all()
        return [self._row(r) for r in rows]

    async def resolve(
        self, tenant: str, thread_id: str, status: str, decision: dict[str, Any]
    ) -> bool:
        """Close a pending review. False if there is none: a decision can be applied once."""
        query = (
            update(reviews)
            .where(
                and_(
                    reviews.c.tenant == tenant,
                    reviews.c.thread_id == thread_id,
                    reviews.c.status == "pending",
                )
            )
            .values(status=status, resolved_at=utcnow(), decision=decision)
        )
        async with self.db.engine.begin() as conn:
            return (await conn.execute(query)).rowcount == 1

    async def delete_thread(self, tenant: str, thread_id: str) -> None:
        query = delete(reviews).where(
            and_(reviews.c.tenant == tenant, reviews.c.thread_id == thread_id)
        )
        async with self.db.engine.begin() as conn:
            await conn.execute(query)

    async def count_pending(self, tenant: str) -> int:
        query = select(func.count()).where(
            and_(reviews.c.tenant == tenant, reviews.c.status == "pending")
        )
        async with self.db.engine.connect() as conn:
            return int((await conn.execute(query)).scalar_one())


class ConsentRegistry:
    def __init__(self, db: Database, audit: AuditLog) -> None:
        self.db = db
        self.audit = audit

    async def record(
        self,
        tenant: str,
        subject_id: str,
        purpose: Purpose,
        granted: bool,
        *,
        source: str,
        actor: str,
    ) -> None:
        key = and_(
            consents.c.tenant == tenant,
            consents.c.subject_id == subject_id,
            consents.c.purpose == purpose.value,
        )
        values = {"granted": granted, "updated_at": utcnow(), "source": source}
        async with self.db.engine.begin() as conn:
            if (await conn.execute(update(consents).where(key).values(**values))).rowcount == 0:
                await conn.execute(
                    insert(consents).values(
                        tenant=tenant, subject_id=subject_id, purpose=purpose.value, **values
                    )
                )
        await self.audit.record(
            tenant,
            actor,
            "consent.granted" if granted else "consent.withdrawn",
            f"consent/{purpose.value}",
            subject_id=subject_id,
            details={"source": source},
        )

    async def get(self, tenant: str, subject_id: str) -> dict[str, dict[str, Any]]:
        query = select(consents).where(
            and_(consents.c.tenant == tenant, consents.c.subject_id == subject_id)
        )
        async with self.db.engine.connect() as conn:
            rows = (await conn.execute(query)).mappings().all()
        return {
            r["purpose"]: {
                "granted": r["granted"],
                "updated_at": (aware(r["updated_at"]) or utcnow()).isoformat(),
                "source": r["source"],
            }
            for r in rows
        }

    async def has(self, tenant: str, subject_id: str, purpose: Purpose) -> bool:
        """No record means no consent (opt-in, never opt-out)."""
        query = select(consents.c.granted).where(
            and_(
                consents.c.tenant == tenant,
                consents.c.subject_id == subject_id,
                consents.c.purpose == purpose.value,
            )
        )
        async with self.db.engine.connect() as conn:
            return bool((await conn.execute(query)).scalar_one_or_none())

    async def granted_subjects(self, tenant: str, purpose: Purpose) -> set[str]:
        query = select(consents.c.subject_id).where(
            and_(
                consents.c.tenant == tenant,
                consents.c.purpose == purpose.value,
                consents.c.granted.is_(True),
            )
        )
        async with self.db.engine.connect() as conn:
            return set((await conn.execute(query)).scalars().all())

    async def erase(self, tenant: str, subject_id: str) -> int:
        """Right to erasure: the consent rows go; the audit events that prove them stay."""
        query = delete(consents).where(
            and_(consents.c.tenant == tenant, consents.c.subject_id == subject_id)
        )
        async with self.db.engine.begin() as conn:
            return (await conn.execute(query)).rowcount
