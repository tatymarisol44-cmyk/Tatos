"""Identity and permissions: who is calling, and what they may do.

Three kinds of principal:

- **service**: the tenant keys in API_KEYS. Meant for the tenant's own backend and for
  bootstrapping (creating staff keys); full rights within the tenant (role `admin`).
  Never hand one to a browser.
- **staff**: one key per person, created by an admin, with explicit roles. The audit
  trail records the principal's id, which comes from the key, never from a header.
- **patient**: a key bound to exactly one subject id, created by reception and sent to
  the patient (e.g. as a link). It only opens the `/v1/me` endpoints, scoped to that
  subject.

Keys are random (`secrets.token_urlsafe`), shown once at creation and stored as SHA-256
hashes: a database leak does not leak usable keys. Keys can expire and be revoked.

Closes audit finding A01: approving, publishing or exporting requires a role carried by
an authenticated key, and the actor cannot be declared by the caller."""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from sqlalchemy import JSON, Column, DateTime, String, Table, and_, insert, select, update

from orchestrator.db import Database, aware, metadata, utcnow
from orchestrator.governance import AuditLog


class Role(StrEnum):
    ADMIN = "admin"  # manage keys and documents; implies every other role
    RECEPTION = "reception"  # patients, appointments, plans, consents, patient access
    REVIEWER = "reviewer"  # resolve answers held for human review (e.g. a dentist)
    OWNER = "owner"  # insights, campaigns, discounts above the pack's cap
    MARKETING = "marketing"  # draft and send campaigns (not approve big discounts)
    PRIVACY = "privacy"  # data-subject export/erasure, audit trail, thread erasure


STAFF = "staff"
SERVICE = "service"
PATIENT = "patient"

principals = Table(
    "principals",
    metadata,
    Column("tenant", String(64), primary_key=True),
    Column("id", String(128), primary_key=True),
    Column("kind", String(16), nullable=False),
    Column("roles", JSON, nullable=False),
    Column("subject_id", String(64), nullable=True),
    Column("key_hash", String(64), nullable=False, unique=True, index=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("created_by", String(128), nullable=False),
    Column("expires_at", DateTime(timezone=True), nullable=True),
    Column("revoked_at", DateTime(timezone=True), nullable=True),
    # "Sign out everywhere" for single sign-on: tokens issued before this are refused.
    Column("not_before", DateTime(timezone=True), nullable=True),
)


@dataclass(frozen=True)
class Principal:
    tenant: str
    id: str
    kind: str
    roles: frozenset[str] = field(default_factory=frozenset)
    subject_id: str | None = None

    def has(self, *roles: str) -> bool:
        """True when the principal holds any of `roles` (admin holds them all)."""
        if self.kind == PATIENT:
            return False
        return Role.ADMIN in self.roles or any(r in self.roles for r in roles)

    @property
    def can_review(self) -> bool:
        return self.has(Role.REVIEWER)


def service_principal(tenant: str) -> Principal:
    return Principal(tenant, f"service:{tenant}", SERVICE, frozenset({Role.ADMIN.value}))


def _hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


class PrincipalStore:
    def __init__(self, db: Database, audit: AuditLog) -> None:
        self.db = db
        self.audit = audit

    async def _create(
        self,
        tenant: str,
        pid: str,
        kind: str,
        roles: list[str],
        subject_id: str | None,
        created_by: str,
        expires_at: datetime | None,
        prefix: str,
    ) -> tuple[dict[str, Any], str]:
        key = f"{prefix}_{secrets.token_urlsafe(32)}"
        async with self.db.engine.begin() as conn:
            existing = (
                await conn.execute(
                    select(principals.c.id).where(
                        and_(principals.c.tenant == tenant, principals.c.id == pid)
                    )
                )
            ).first()
            if existing is not None:
                raise ValueError(f"principal {pid!r} already exists")
            await conn.execute(
                insert(principals).values(
                    tenant=tenant,
                    id=pid,
                    kind=kind,
                    roles=sorted(roles),
                    subject_id=subject_id,
                    key_hash=_hash(key),
                    created_at=utcnow(),
                    created_by=created_by,
                    expires_at=expires_at,
                )
            )
            # Same transaction: no key exists without the audit event that created it.
            await self.audit.record_in(
                conn,
                tenant,
                created_by,
                f"principal.{kind}.created",
                f"principal/{pid}",
                subject_id=subject_id,
                details={"roles": sorted(roles)},
            )
        info = {
            "id": pid,
            "kind": kind,
            "roles": sorted(roles),
            "subject_id": subject_id,
            "expires_at": expires_at.isoformat() if expires_at else None,
        }
        return info, key

    async def create_staff(
        self, tenant: str, name: str, roles: list[Role], created_by: str
    ) -> tuple[dict[str, Any], str]:
        if not roles:
            raise ValueError("a staff key needs at least one role")
        return await self._create(
            tenant, name, STAFF, [r.value for r in roles], None, created_by, None, "sk"
        )

    async def create_patient_access(
        self, tenant: str, subject_id: str, created_by: str, ttl_days: int
    ) -> tuple[dict[str, Any], str]:
        pid = f"patient:{subject_id}:{secrets.token_hex(4)}"
        expires = utcnow() + timedelta(days=ttl_days)
        return await self._create(tenant, pid, PATIENT, [], subject_id, created_by, expires, "pk")

    async def resolve(self, key: str) -> Principal | None:
        query = select(principals).where(principals.c.key_hash == _hash(key))
        async with self.db.engine.connect() as conn:
            row = (await conn.execute(query)).mappings().first()
        if row is None or row["revoked_at"] is not None:
            return None
        expires = aware(row["expires_at"])
        if expires is not None and expires <= utcnow():
            return None
        return Principal(
            row["tenant"], row["id"], row["kind"], frozenset(row["roles"]), row["subject_id"]
        )

    async def from_identity(self, identity: Any) -> Principal | None:
        """The staff principal of a verified single-sign-on identity (oidc.Identity),
        provisioned on first sign-in. Roles follow the identity provider on every
        sign-in. None when the principal was revoked, or signed out after the token was
        issued. Raises ValueError when the id belongs to a non-staff principal."""
        key = and_(principals.c.tenant == identity.tenant, principals.c.id == identity.id)
        roles = sorted(identity.roles)
        async with self.db.engine.begin() as conn:
            row = (await conn.execute(select(principals).where(key))).mappings().first()
            if row is None:
                await conn.execute(
                    insert(principals).values(
                        tenant=identity.tenant,
                        id=identity.id,
                        kind=STAFF,
                        roles=roles,
                        subject_id=None,
                        # No usable key: the hash of a random value nobody ever sees.
                        key_hash=_hash(f"oidc:{secrets.token_urlsafe(32)}"),
                        created_at=utcnow(),
                        created_by="oidc",
                        expires_at=None,
                    )
                )
                await self.audit.record_in(
                    conn,
                    identity.tenant,
                    identity.id,
                    "principal.staff.provisioned",
                    f"principal/{identity.id}",
                    details={"roles": roles, "via": "oidc", "mfa": identity.mfa},
                )
            else:
                if row["kind"] != STAFF:
                    raise ValueError("this identity is not a staff member")
                if row["revoked_at"] is not None:
                    return None
                not_before = aware(row["not_before"])
                if not_before is not None and identity.issued_at < not_before.timestamp():
                    return None
                if sorted(row["roles"]) != roles:
                    await conn.execute(update(principals).where(key).values(roles=roles))
                    await self.audit.record_in(
                        conn,
                        identity.tenant,
                        "oidc",
                        "principal.roles_synced",
                        f"principal/{identity.id}",
                        details={"from": sorted(row["roles"]), "to": roles},
                    )
        return Principal(identity.tenant, identity.id, STAFF, frozenset(roles))

    async def sign_out(self, tenant: str, pid: str, actor: str) -> bool:
        """End every single-sign-on session of a staff member (lost laptop, stolen
        phone): tokens issued before now stop working; a new sign-in works again."""
        query = (
            update(principals)
            .where(
                and_(
                    principals.c.tenant == tenant,
                    principals.c.id == pid,
                    principals.c.kind == STAFF,
                    principals.c.revoked_at.is_(None),
                )
            )
            .values(not_before=utcnow())
        )
        async with self.db.engine.begin() as conn:
            done = (await conn.execute(query)).rowcount == 1
            if done:
                await self.audit.record_in(
                    conn, tenant, actor, "principal.signed_out", f"principal/{pid}"
                )
        return done

    async def list(self, tenant: str) -> list[dict[str, Any]]:
        query = select(
            principals.c.id,
            principals.c.kind,
            principals.c.roles,
            principals.c.subject_id,
            principals.c.created_at,
            principals.c.created_by,
            principals.c.expires_at,
            principals.c.revoked_at,
        ).where(principals.c.tenant == tenant)
        async with self.db.engine.connect() as conn:
            rows = (await conn.execute(query.order_by(principals.c.created_at))).mappings().all()
        out = []
        for r in rows:
            item = dict(r)
            for k in ("created_at", "expires_at", "revoked_at"):
                value = aware(item[k])
                item[k] = value.isoformat() if value else None
            out.append(item)
        return out

    async def revoke(self, tenant: str, pid: str, actor: str) -> bool:
        query = (
            update(principals)
            .where(
                and_(
                    principals.c.tenant == tenant,
                    principals.c.id == pid,
                    principals.c.revoked_at.is_(None),
                )
            )
            .values(revoked_at=utcnow())
        )
        async with self.db.engine.begin() as conn:
            done = (await conn.execute(query)).rowcount == 1
            if done:
                await self.audit.record_in(
                    conn, tenant, actor, "principal.revoked", f"principal/{pid}"
                )
        return done

    async def revoke_subject(self, tenant: str, subject_id: str, actor: str) -> int:
        """Erasure: a patient's access keys stop working."""
        query = (
            update(principals)
            .where(
                and_(
                    principals.c.tenant == tenant,
                    principals.c.subject_id == subject_id,
                    principals.c.revoked_at.is_(None),
                )
            )
            .values(revoked_at=utcnow())
        )
        async with self.db.engine.begin() as conn:
            return int((await conn.execute(query)).rowcount)
