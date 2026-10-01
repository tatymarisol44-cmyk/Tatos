"""Governance records (the ERM data layer): audit trail, human-review queue, consents.

- Audit trail: append-only events (who did what to which data subject). Kept apart from
  conversations, which are purged after THREAD_RETENTION_DAYS: HIPAA asks for audit
  records to be kept for six years, and erasure requests must stay provable. Each event
  carries the hash of the previous one (per tenant), so `verify` detects any event that
  was edited, deleted or inserted afterwards: the log does not rely on the application
  that writes it being honest later.
- Review queue: answers paused by the graph for a human decision (interrupt + checkpoint).
  pending -> resolving (claimed by one reviewer, graph resuming) -> approved | rejected.
- Thread leases: one active run per conversation, across replicas.
- Subject threads: which conversations belong to which data subject (export, erasure).
- Consents: one current state per (tenant, subject, purpose); every change is also an
  audit event, which is the proof of consent GDPR Art. 7(1) asks for.

Subjects are identified by a pseudonymous `subject_id` chosen by the tenant (a patient or
customer number), never by name or e-mail."""

from __future__ import annotations

import builtins
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
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
    UniqueConstraint,
    and_,
    delete,
    func,
    insert,
    select,
    update,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from orchestrator.db import Database, aware, metadata, utcnow


class Purpose(StrEnum):
    """What a subject consented to. Treatment itself rests on a different legal basis
    (GDPR Art. 9(2)(h), HIPAA treatment/operations); it is recorded for completeness."""

    TREATMENT = "treatment"
    MARKETING = "marketing"  # campaigns, reminders beyond the appointment itself
    MEMORY = "memory"  # long-term semantic memory of preferences
    PHOTOS = "photos"  # before/after images in marketing
    ANALYTICS = "analytics"  # profiling: segments, value ranking, campaign targeting


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
    # Hash chain, per tenant: seq 1, 2, 3...; hash = sha256(prev_hash + canonical event).
    Column("seq", Integer, nullable=False),
    Column("prev_hash", String(64), nullable=False),
    Column("hash", String(64), nullable=False),
    UniqueConstraint("tenant", "seq", name="uq_audit_tenant_seq"),
)

# Head of each tenant's chain. Writers increment it with a row-locking UPDATE, so two
# concurrent transactions (on any replica) can never append the same seq.
audit_heads = Table(
    "audit_heads",
    metadata,
    Column("tenant", String(64), primary_key=True),
    Column("seq", Integer, nullable=False),
    Column("hash", String(64), nullable=False),
)

GENESIS = "0" * 64

reviews = Table(
    "reviews",
    metadata,
    Column("tenant", String(64), primary_key=True),
    Column("thread_id", String(128), primary_key=True),
    # pending | resolving | approved | rejected
    Column("status", String(16), nullable=False, index=True),
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

thread_leases = Table(
    "thread_leases",
    metadata,
    Column("tenant", String(64), primary_key=True),
    Column("thread_id", String(128), primary_key=True),
    Column("holder", String(64), nullable=False),
    Column("expires_at", DateTime(timezone=True), nullable=False),
)

subject_threads = Table(
    "subject_threads",
    metadata,
    Column("tenant", String(64), primary_key=True),
    Column("thread_id", String(128), primary_key=True),
    Column("subject_id", String(64), nullable=False, index=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
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
    seq: int = 0
    prev_hash: str = GENESIS
    hash: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {**self.__dict__, "ts": self.ts.isoformat()}


def _event(row: Any) -> AuditEvent:
    return AuditEvent(**{**row, "ts": aware(row["ts"]), "details": row["details"] or {}})


def _canonical(details: dict[str, Any] | None) -> dict[str, Any]:
    """The JSON form a details dict has after a database round trip (string keys, lists,
    ISO dates), so the hash computed on write matches the one recomputed on read."""
    result: dict[str, Any] = json.loads(json.dumps(details or {}, default=str))
    return result


def event_hash(
    prev_hash: str,
    seq: int,
    ts: datetime,
    tenant: str,
    actor: str,
    action: str,
    resource: str,
    subject_id: str | None,
    details: dict[str, Any],
) -> str:
    body = json.dumps(
        [prev_hash, seq, ts.isoformat(), tenant, actor, action, resource, subject_id, details],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(body.encode()).hexdigest()


class AuditChainBusyError(RuntimeError):
    """Two writers created the first event of a tenant at the same time; retry."""


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
        head = (
            await conn.execute(
                update(audit_heads)
                .where(audit_heads.c.tenant == tenant)
                .values(seq=audit_heads.c.seq + 1)
                .returning(audit_heads.c.seq, audit_heads.c.hash)
            )
        ).first()
        if head is None:  # first event of this tenant
            seq, prev = 1, GENESIS
            try:
                await conn.execute(insert(audit_heads).values(tenant=tenant, seq=1, hash=prev))
            except IntegrityError as exc:  # the whole transaction rolls back; retry it
                raise AuditChainBusyError(tenant) from exc
        else:
            seq, prev = int(head.seq), str(head.hash)
        ts = utcnow()
        body = _canonical(details)
        digest = event_hash(prev, seq, ts, tenant, actor, action, resource, subject_id, body)
        await conn.execute(
            insert(audit_events).values(
                ts=ts,
                tenant=tenant,
                actor=actor,
                action=action,
                resource=resource,
                subject_id=subject_id,
                details=body,
                seq=seq,
                prev_hash=prev,
                hash=digest,
            )
        )
        await conn.execute(
            update(audit_heads).where(audit_heads.c.tenant == tenant).values(hash=digest)
        )

    async def verify(self, tenant: str) -> dict[str, Any]:
        """Recompute the tenant's chain. Any edited, deleted, reordered or inserted event
        (or a truncated tail) breaks it; `broken_at` names the first bad seq."""
        prev, expected = GENESIS, 1
        async with self.db.engine.connect() as conn:
            head = (
                await conn.execute(select(audit_heads).where(audit_heads.c.tenant == tenant))
            ).first()
            while True:  # keyset pages: a six-year log is never loaded at once
                rows = (
                    (
                        await conn.execute(
                            select(audit_events)
                            .where(
                                and_(
                                    audit_events.c.tenant == tenant, audit_events.c.seq >= expected
                                )
                            )
                            .order_by(audit_events.c.seq)
                            .limit(1000)
                        )
                    )
                    .mappings()
                    .all()
                )
                for ev in rows:
                    digest = event_hash(
                        prev,
                        ev["seq"],
                        aware(ev["ts"]) or utcnow(),
                        ev["tenant"],
                        ev["actor"],
                        ev["action"],
                        ev["resource"],
                        ev["subject_id"],
                        _canonical(ev["details"]),
                    )
                    if ev["seq"] != expected or ev["prev_hash"] != prev or ev["hash"] != digest:
                        return {"ok": False, "events": expected - 1, "broken_at": expected}
                    prev, expected = digest, expected + 1
                if len(rows) < 1000:
                    break
        count = expected - 1
        head_state = (int(head.seq), str(head.hash)) if head is not None else (0, GENESIS)
        if head_state != (count, prev):  # events missing at the end of the chain
            return {"ok": False, "events": count, "broken_at": count + 1}
        return {"ok": True, "events": count, "broken_at": None, "head": prev}

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
        return [_event(row) for row in rows]

    async def all_for_subject(
        self, tenant: str, subject_id: str, page_size: int = 500
    ) -> builtins.list[AuditEvent]:
        """Every event of a subject, oldest first, read in keyset pages so a long history
        is exported whole without one unbounded query (audit finding A05)."""
        out: builtins.list[AuditEvent] = []
        last = 0
        while True:
            query = (
                select(audit_events)
                .where(
                    and_(
                        audit_events.c.tenant == tenant,
                        audit_events.c.subject_id == subject_id,
                        audit_events.c.id > last,
                    )
                )
                .order_by(audit_events.c.id)
                .limit(page_size)
            )
            async with self.db.engine.connect() as conn:
                rows = (await conn.execute(query)).mappings().all()
            out.extend(_event(r) for r in rows)
            if len(rows) < page_size:
                return out
            last = rows[-1]["id"]


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

    async def _move(
        self,
        tenant: str,
        thread_id: str,
        frm: str,
        to: str,
        conn: AsyncConnection | None = None,
        **values: Any,
    ) -> bool:
        query = (
            update(reviews)
            .where(
                and_(
                    reviews.c.tenant == tenant,
                    reviews.c.thread_id == thread_id,
                    reviews.c.status == frm,
                )
            )
            .values(status=to, **values)
        )
        if conn is not None:
            return (await conn.execute(query)).rowcount == 1
        async with self.db.engine.begin() as own:
            return (await own.execute(query)).rowcount == 1

    async def claim(self, tenant: str, thread_id: str, decision: dict[str, Any]) -> bool:
        """pending -> resolving. One conditional UPDATE: of two reviewers (on any replica)
        exactly one wins. The decision is stored now so a crash can be reconciled."""
        return await self._move(
            tenant, thread_id, "pending", "resolving", resolved_at=utcnow(), decision=decision
        )

    async def finish(
        self, tenant: str, thread_id: str, status: str, conn: AsyncConnection | None = None
    ) -> bool:
        """resolving -> approved | rejected, once the graph has finished the run."""
        return await self._move(tenant, thread_id, "resolving", status, conn)

    async def release(self, tenant: str, thread_id: str) -> bool:
        """resolving -> pending: the resume failed and the thread is still paused."""
        return await self._move(
            tenant, thread_id, "resolving", "pending", resolved_at=None, decision=None
        )

    async def stale_resolving(self, older_than: timedelta) -> builtins.list[Review]:
        query = select(reviews).where(
            and_(reviews.c.status == "resolving", reviews.c.resolved_at < utcnow() - older_than)
        )
        async with self.db.engine.connect() as conn:
            rows = (await conn.execute(query)).mappings().all()
        return [self._row(r) for r in rows]

    async def for_subject(self, tenant: str, subject_id: str) -> builtins.list[Review]:
        query = select(reviews).where(
            and_(reviews.c.tenant == tenant, reviews.c.subject_id == subject_id)
        )
        async with self.db.engine.connect() as conn:
            rows = (await conn.execute(query)).mappings().all()
        return [self._row(r) for r in rows]

    async def delete_thread(self, tenant: str, thread_id: str) -> None:
        query = delete(reviews).where(
            and_(reviews.c.tenant == tenant, reviews.c.thread_id == thread_id)
        )
        async with self.db.engine.begin() as conn:
            await conn.execute(query)

    async def is_open(self, tenant: str, thread_id: str) -> bool:
        review = await self.get(tenant, thread_id)
        return review is not None and review.status in ("pending", "resolving")

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
            await self.audit.record_in(
                conn,
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


class ThreadBusyError(RuntimeError):
    """Another run of this conversation is in progress (here or on another replica)."""


class ThreadLeases:
    """One active run per conversation. The lease is a row, so it holds across replicas;
    it expires, so a replica that crashed mid-run does not block the thread forever."""

    def __init__(self, db: Database, ttl: timedelta) -> None:
        self.db = db
        self.ttl = ttl

    async def acquire(self, tenant: str, thread_id: str, holder: str) -> None:
        now = utcnow()
        values = {"holder": holder, "expires_at": now + self.ttl}
        try:
            async with self.db.engine.begin() as conn:
                await conn.execute(
                    insert(thread_leases).values(tenant=tenant, thread_id=thread_id, **values)
                )
            return
        except IntegrityError:
            pass
        takeover = (
            update(thread_leases)
            .where(
                and_(
                    thread_leases.c.tenant == tenant,
                    thread_leases.c.thread_id == thread_id,
                    thread_leases.c.expires_at < now,
                )
            )
            .values(**values)
        )
        async with self.db.engine.begin() as conn:
            if (await conn.execute(takeover)).rowcount == 1:
                return
        raise ThreadBusyError(f"thread {thread_id} has a run in progress")

    async def release(self, tenant: str, thread_id: str, holder: str) -> None:
        query = delete(thread_leases).where(
            and_(
                thread_leases.c.tenant == tenant,
                thread_leases.c.thread_id == thread_id,
                thread_leases.c.holder == holder,
            )
        )
        async with self.db.engine.begin() as conn:
            await conn.execute(query)


class SubjectThreads:
    """Index of the conversations held about each data subject. Checkpoints are opaque
    blobs; without this index an export or an erasure could not find them (A05)."""

    def __init__(self, db: Database) -> None:
        self.db = db

    async def link(self, tenant: str, thread_id: str, subject_id: str) -> None:
        try:
            async with self.db.engine.begin() as conn:
                await conn.execute(
                    insert(subject_threads).values(
                        tenant=tenant,
                        thread_id=thread_id,
                        subject_id=subject_id,
                        created_at=utcnow(),
                    )
                )
        except IntegrityError:
            pass  # already linked (the thread is bound to one subject by the service)

    async def threads(self, tenant: str, subject_id: str) -> list[str]:
        query = (
            select(subject_threads.c.thread_id)
            .where(
                and_(subject_threads.c.tenant == tenant, subject_threads.c.subject_id == subject_id)
            )
            .order_by(subject_threads.c.created_at)
        )
        async with self.db.engine.connect() as conn:
            return list((await conn.execute(query)).scalars().all())

    async def unlink(self, tenant: str, thread_id: str) -> None:
        query = delete(subject_threads).where(
            and_(subject_threads.c.tenant == tenant, subject_threads.c.thread_id == thread_id)
        )
        async with self.db.engine.begin() as conn:
            await conn.execute(query)
