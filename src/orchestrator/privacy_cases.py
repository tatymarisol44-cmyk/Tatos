"""Privacy cases: data-subject requests and personal-data breaches, with their legal
deadlines (LOPDP; table "Deadlines" in docs/legal/README.md).

* **A case is a list of steps, each with a due date** computed when the case opens:
  - access, rectification, erasure, objection: answer within 15 days (Arts. 13-16);
  - breach: the platform tells the clinic within 2 days when the platform found it
    (Art. 43; SPDP-2025-0006); the clinic tells the SPDP and ARCOTEL within 5 days
    (Art. 43) and the affected patients within 3 days when their rights are at risk
    (Art. 46). "Not required" is a valid outcome of that last step, with its reason.
* **Days are calendar days**, counted from when the request arrived or the breach became
  known. The law speaks of *término* (working days) for breaches; calendar days are never
  later, so a date shown here is always safe to meet (lawyer to confirm, docs/legal).
* **No clinical content.** The summary and outcomes say what happened ("export sent by
  e-mail", "SPDP form filed, ref ..."), never what the record contains.
* Every change is audited; `due_soon` feeds the `PrivacyDeadlineAtRisk` alert.

Fulfilling the request itself stays where it is (`/v1/subjects/{id}/export`, erasure...):
this register proves that it was done, and in time."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Column, DateTime, String, Table, and_, func, insert, select, update

from orchestrator.db import Database, aware, metadata, utcnow
from orchestrator.governance import AuditLog

RIGHTS = {"access": "13", "rectification": "14", "erasure": "15", "objection": "16"}
BREACH = "breach"
KINDS = (*RIGHTS, BREACH)
DUE_SOON = timedelta(hours=24)
MAX_TEXT = 500

privacy_cases = Table(
    "privacy_cases",
    metadata,
    Column("tenant", String(64), primary_key=True),
    Column("case_id", String(32), primary_key=True),
    Column("kind", String(16), nullable=False),
    Column("subject_id", String(64), nullable=True),
    Column("summary", String(MAX_TEXT), nullable=False),
    Column("opened_at", DateTime(timezone=True), nullable=False),  # received / became known
    Column("status", String(16), nullable=False),  # open | closed
    Column("created_by", String(128), nullable=False),
    Column("closed_at", DateTime(timezone=True), nullable=True),
    Column("closed_by", String(128), nullable=True),
)

privacy_case_steps = Table(
    "privacy_case_steps",
    metadata,
    Column("tenant", String(64), primary_key=True),
    Column("case_id", String(32), primary_key=True),
    Column("step", String(32), primary_key=True),
    Column("legal_basis", String(80), nullable=False),
    Column("due_at", DateTime(timezone=True), nullable=False),
    Column("done_at", DateTime(timezone=True), nullable=True),
    Column("done_by", String(128), nullable=True),
    Column("outcome", String(MAX_TEXT), nullable=True),
)


class PrivacyCaseError(ValueError):
    """A request the register cannot accept; the message says why."""


def steps_for(kind: str, *, found_by_platform: bool) -> list[tuple[str, int, str]]:
    """(step, days, legal basis) of a new case."""
    if kind in RIGHTS:
        return [("answer", 15, f"LOPDP Art. {RIGHTS[kind]}")]
    if kind != BREACH:
        raise PrivacyCaseError(f"unknown kind {kind!r}; one of {', '.join(KINDS)}")
    steps = [
        ("notify_authority", 5, "LOPDP Art. 43 (SPDP and ARCOTEL)"),
        ("notify_subjects", 3, "LOPDP Art. 46 (when rights are at risk)"),
    ]
    if found_by_platform:
        steps.insert(0, ("notify_controller", 2, "LOPDP Art. 43; SPDP-2025-0006"))
    return steps


def _text(value: str, what: str) -> str:
    value = value.strip()
    if not value or len(value) > MAX_TEXT:
        raise PrivacyCaseError(f"{what} must have 1 to {MAX_TEXT} characters")
    return value


class PrivacyCases:
    def __init__(self, db: Database, audit: AuditLog) -> None:
        self.db = db
        self.audit = audit

    async def open(
        self,
        tenant: str,
        actor: str,
        *,
        kind: str,
        summary: str,
        subject_id: str | None = None,
        opened_at: datetime | None = None,
        found_by_platform: bool = False,
    ) -> dict[str, Any]:
        now = utcnow()
        opened = aware(opened_at) or now
        if opened > now + timedelta(minutes=5):
            raise PrivacyCaseError("opened_at is in the future")
        if kind in RIGHTS and not subject_id:
            raise PrivacyCaseError("a rights request needs the subject_id it is about")
        steps = steps_for(kind, found_by_platform=found_by_platform)
        case_id = uuid.uuid4().hex[:16]
        async with self.db.engine.begin() as conn:
            await conn.execute(
                insert(privacy_cases).values(
                    tenant=tenant,
                    case_id=case_id,
                    kind=kind,
                    subject_id=subject_id,
                    summary=_text(summary, "summary"),
                    opened_at=opened,
                    status="open",
                    created_by=actor,
                )
            )
            for step, days, basis in steps:
                await conn.execute(
                    insert(privacy_case_steps).values(
                        tenant=tenant,
                        case_id=case_id,
                        step=step,
                        legal_basis=basis,
                        due_at=opened + timedelta(days=days),
                    )
                )
            await self.audit.record_in(
                conn,
                tenant,
                actor,
                "privacy_case.opened",
                f"privacy_case/{case_id}",
                subject_id=subject_id,
                details={"kind": kind, "steps": [s for s, _, _ in steps]},
            )
        return await self.get(tenant, case_id)

    async def get(self, tenant: str, case_id: str) -> dict[str, Any]:
        found = await self.list(tenant, case_id=case_id)
        if not found:
            raise KeyError(case_id)
        return found[0]

    async def list(
        self, tenant: str, *, status: str | None = None, case_id: str | None = None
    ) -> list[dict[str, Any]]:
        cases = select(privacy_cases).where(privacy_cases.c.tenant == tenant)
        steps = select(privacy_case_steps).where(privacy_case_steps.c.tenant == tenant)
        if status:
            cases = cases.where(privacy_cases.c.status == status)
        if case_id:
            cases = cases.where(privacy_cases.c.case_id == case_id)
            steps = steps.where(privacy_case_steps.c.case_id == case_id)
        async with self.db.engine.connect() as conn:
            rows = (await conn.execute(cases.order_by(privacy_cases.c.opened_at))).all()
            step_rows = (await conn.execute(steps.order_by(privacy_case_steps.c.due_at))).all()
        now = utcnow()
        by_case: dict[str, list[dict[str, Any]]] = {}
        for s in step_rows:
            due, done = aware(s.due_at), aware(s.done_at)
            assert due is not None
            by_case.setdefault(s.case_id, []).append(
                {
                    "step": s.step,
                    "legal_basis": s.legal_basis,
                    "due_at": due.isoformat(),
                    "done_at": done.isoformat() if done else None,
                    "done_by": s.done_by,
                    "outcome": s.outcome,
                    "late": (done or now) > due,
                    "hours_left": None if done else round((due - now).total_seconds() / 3600, 1),
                }
            )
        out = []
        for r in rows:
            opened, closed = aware(r.opened_at), aware(r.closed_at)
            assert opened is not None
            out.append(
                {
                    "case_id": r.case_id,
                    "kind": r.kind,
                    "subject_id": r.subject_id,
                    "summary": r.summary,
                    "opened_at": opened.isoformat(),
                    "status": r.status,
                    "created_by": r.created_by,
                    "closed_at": closed.isoformat() if closed else None,
                    "closed_by": r.closed_by,
                    "steps": by_case.get(r.case_id, []),
                }
            )
        return out

    async def complete_step(
        self, tenant: str, case_id: str, step: str, actor: str, outcome: str
    ) -> dict[str, Any]:
        outcome = _text(outcome, "outcome")
        key = and_(
            privacy_case_steps.c.tenant == tenant,
            privacy_case_steps.c.case_id == case_id,
            privacy_case_steps.c.step == step,
        )
        async with self.db.engine.begin() as conn:
            done = await conn.execute(
                update(privacy_case_steps)
                .where(and_(key, privacy_case_steps.c.done_at.is_(None)))
                .values(done_at=utcnow(), done_by=actor, outcome=outcome)
            )
            if done.rowcount != 1:
                exists = await conn.scalar(
                    select(func.count()).select_from(privacy_case_steps).where(key)
                )
                if not exists:
                    raise KeyError(f"{case_id}/{step}")
                raise PrivacyCaseError(f"step {step} is already done")
            await self.audit.record_in(
                conn,
                tenant,
                actor,
                "privacy_case.step_done",
                f"privacy_case/{case_id}",
                details={"step": step},
            )
        return await self.get(tenant, case_id)

    async def close(self, tenant: str, case_id: str, actor: str) -> dict[str, Any]:
        case = await self.get(tenant, case_id)
        if case["status"] != "open":
            raise PrivacyCaseError("the case is already closed")
        pending = [s["step"] for s in case["steps"] if not s["done_at"]]
        if pending:
            raise PrivacyCaseError(f"steps still open: {', '.join(pending)}")
        async with self.db.engine.begin() as conn:
            await conn.execute(
                update(privacy_cases)
                .where(
                    and_(
                        privacy_cases.c.tenant == tenant,
                        privacy_cases.c.case_id == case_id,
                        privacy_cases.c.status == "open",
                    )
                )
                .values(status="closed", closed_at=utcnow(), closed_by=actor)
            )
            late = [s["step"] for s in case["steps"] if s["late"]]
            await self.audit.record_in(
                conn,
                tenant,
                actor,
                "privacy_case.closed",
                f"privacy_case/{case_id}",
                details={"late_steps": late},
            )
        return await self.get(tenant, case_id)

    async def due_soon(self) -> int:
        """Open steps, all tenants, due within 24 hours or already late (alert input)."""
        query = (
            select(func.count())
            .select_from(privacy_case_steps)
            .where(
                privacy_case_steps.c.done_at.is_(None),
                privacy_case_steps.c.due_at <= utcnow() + DUE_SOON,
            )
        )
        async with self.db.engine.connect() as conn:
            return int(await conn.scalar(query) or 0)
