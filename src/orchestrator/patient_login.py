"""Patient accounts, activated by a private clinic-issued link. Never staff privileges.

Email is a login identifier (not proof of mailbox ownership), unique per deployment.
Recovery is issued by reception after identifying the patient; no mail is sent here.
"""

from __future__ import annotations

import asyncio
import hashlib
import secrets
from datetime import timedelta
from typing import Any

from sqlalchemy import (
    Column,
    DateTime,
    Integer,
    String,
    Table,
    UniqueConstraint,
    and_,
    delete,
    insert,
    select,
    update,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from orchestrator.auth import PATIENT, Principal, PrincipalStore, principals
from orchestrator.crm import patients
from orchestrator.db import Database, aware, metadata, utcnow
from orchestrator.governance import AuditLog
from orchestrator.staff_login import (
    check_password,
    hash_password,
    normalize,
    password_problem,
    valid_email,
)

patient_logins = Table(
    "patient_logins",
    metadata,
    Column("tenant", String(64), primary_key=True),
    Column("patient_id", String(64), primary_key=True),
    Column("email", String(254), nullable=False, unique=True, index=True),
    Column("password_hash", String(200), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("failed_attempts", Integer, nullable=False, default=0),
    Column("locked_until", DateTime(timezone=True)),
    Column("recovery_hash", String(64)),
)
patient_sessions = Table(
    "patient_sessions",
    metadata,
    Column("tenant", String(64), primary_key=True),
    Column("key_hash", String(64), primary_key=True),
    Column("patient_id", String(64), nullable=False),
    Column("expires_at", DateTime(timezone=True), nullable=False, index=True),
    UniqueConstraint("key_hash", name="uq_patient_sessions_key_hash"),
)
PREFIX = "ps_"


class PatientLoginError(ValueError):
    pass


def digest(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


class PatientLogins:
    def __init__(self, db: Database, audit: AuditLog, keys: PrincipalStore) -> None:
        self.db, self.audit, self.keys = db, audit, keys

    @staticmethod
    def identity(table: Table, tenant: str, patient: str) -> Any:
        return and_(table.c.tenant == tenant, table.c.patient_id == patient)

    async def _session(self, conn: AsyncConnection, tenant: str, patient: str) -> dict[str, str]:
        key = PREFIX + secrets.token_urlsafe(32)
        expires = utcnow() + timedelta(hours=12)
        await conn.execute(
            insert(patient_sessions).values(
                key_hash=digest(key), tenant=tenant, patient_id=patient, expires_at=expires
            )
        )
        await self.audit.record_in(
            conn,
            tenant,
            f"patient:{patient}",
            "patient.session.opened",
            f"patient/{patient}",
            subject_id=patient,
        )
        return {"key": key, "expires_at": expires.isoformat()}

    async def info(self, p: Principal, key: str) -> dict[str, Any]:
        async with self.db.engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        select(patient_logins).where(
                            self.identity(patient_logins, p.tenant, str(p.subject_id))
                        )
                    )
                )
                .mappings()
                .first()
            )
        return {
            "email": row["email"] if row else None,
            "can_activate": key.startswith("pk_")
            and (row is None or row["recovery_hash"] == digest(key)),
        }

    async def activate(self, p: Principal, key: str, email: str, password: str) -> dict[str, str]:
        if not key.startswith("pk_") or not p.subject_id:
            raise PatientLoginError("Abre el enlace de activación o recuperación del consultorio.")
        email = normalize(email)
        if not valid_email(email):
            raise PatientLoginError("Escribe un correo válido.")
        problem = password_problem(password, email)
        if problem:
            raise PatientLoginError(problem)
        hashed = await asyncio.to_thread(hash_password, password)
        pid = p.subject_id
        try:
            async with self.db.engine.begin() as conn:
                patient = (
                    (
                        await conn.execute(
                            select(patients)
                            .where(and_(patients.c.tenant == p.tenant, patients.c.id == pid))
                            .with_for_update()
                        )
                    )
                    .mappings()
                    .first()
                )
                if patient is None or patient["restricted"]:
                    raise PatientLoginError("La ficha no está disponible.")
                link = (
                    await conn.execute(
                        select(principals).where(
                            and_(
                                principals.c.tenant == p.tenant,
                                principals.c.id == p.id,
                                principals.c.kind == PATIENT,
                                principals.c.subject_id == pid,
                                principals.c.key_hash == digest(key),
                                principals.c.revoked_at.is_(None),
                                principals.c.expires_at > utcnow(),
                            )
                        )
                    )
                ).first()
                if link is None:
                    raise PatientLoginError("El enlace venció o ya fue utilizado.")
                condition = self.identity(patient_logins, p.tenant, pid)
                old = (
                    (await conn.execute(select(patient_logins).where(condition).with_for_update()))
                    .mappings()
                    .first()
                )
                if old and old["recovery_hash"] != digest(key):
                    raise PatientLoginError(
                        "La cuenta ya está activada. Pide un enlace de recuperación."
                    )
                values = dict(
                    email=email,
                    password_hash=hashed,
                    failed_attempts=0,
                    locked_until=None,
                    recovery_hash=None,
                )
                if old:
                    await conn.execute(update(patient_logins).where(condition).values(**values))
                else:
                    await conn.execute(
                        insert(patient_logins).values(
                            tenant=p.tenant, patient_id=pid, created_at=utcnow(), **values
                        )
                    )
                await conn.execute(
                    update(patients)
                    .where(and_(patients.c.tenant == p.tenant, patients.c.id == pid))
                    .values(email=email)
                )
                await conn.execute(
                    delete(patient_sessions).where(self.identity(patient_sessions, p.tenant, pid))
                )
                await conn.execute(
                    update(principals)
                    .where(
                        and_(
                            principals.c.tenant == p.tenant,
                            principals.c.subject_id == pid,
                            principals.c.kind == PATIENT,
                        )
                    )
                    .values(revoked_at=utcnow())
                )
                await self.audit.record_in(
                    conn,
                    p.tenant,
                    p.id,
                    "patient.account.activated",
                    f"patient/{pid}",
                    subject_id=pid,
                )
                return await self._session(conn, p.tenant, pid)
        except IntegrityError as exc:
            raise PatientLoginError(
                "No se puede usar ese correo. Revisa los datos con el consultorio."
            ) from exc

    async def sign_in(self, email: str, password: str) -> dict[str, str]:
        result = None
        async with self.db.engine.begin() as conn:
            row = (
                (
                    await conn.execute(
                        select(patient_logins)
                        .where(patient_logins.c.email == normalize(email))
                        .with_for_update()
                    )
                )
                .mappings()
                .first()
            )
            good = await asyncio.to_thread(
                check_password, password, row["password_hash"] if row else None
            )
            if row:
                locked = aware(row["locked_until"])
                condition = self.identity(patient_logins, row["tenant"], row["patient_id"])
                patient = (
                    await conn.execute(
                        select(patients.c.restricted).where(
                            and_(
                                patients.c.tenant == row["tenant"],
                                patients.c.id == row["patient_id"],
                            )
                        )
                    )
                ).first()
                if patient and not patient.restricted and (locked is None or locked <= utcnow()):
                    if good:
                        await conn.execute(
                            update(patient_logins)
                            .where(condition)
                            .values(failed_attempts=0, locked_until=None)
                        )
                        result = await self._session(conn, row["tenant"], row["patient_id"])
                    else:
                        attempts = (0 if locked else row["failed_attempts"]) + 1
                        await conn.execute(
                            update(patient_logins)
                            .where(condition)
                            .values(
                                failed_attempts=attempts,
                                locked_until=utcnow() + timedelta(minutes=15)
                                if attempts >= 5
                                else None,
                            )
                        )
        if result is None:
            raise PatientLoginError(
                "Correo o contraseña incorrectos, o acceso temporalmente bloqueado."
            )
        return result

    async def resolve(self, key: str) -> Principal | None:
        async with self.db.engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        select(patient_sessions)
                        .join(
                            patients,
                            and_(
                                patients.c.tenant == patient_sessions.c.tenant,
                                patients.c.id == patient_sessions.c.patient_id,
                            ),
                        )
                        .where(
                            and_(
                                patient_sessions.c.key_hash == digest(key),
                                patient_sessions.c.expires_at > utcnow(),
                                patients.c.restricted.is_(False),
                            )
                        )
                    )
                )
                .mappings()
                .first()
            )
        if row is None:
            return None
        return Principal(
            row["tenant"],
            f"patient-account:{row['patient_id']}",
            PATIENT,
            subject_id=row["patient_id"],
        )

    async def sign_out(self, key: str) -> None:
        async with self.db.engine.begin() as conn:
            await conn.execute(
                delete(patient_sessions).where(patient_sessions.c.key_hash == digest(key))
            )

    async def recovery(self, tenant: str, patient: str, actor: str) -> dict[str, str]:
        # Creation alone grants no password reset: only the latest stored hash does.
        key = "pk_" + secrets.token_urlsafe(32)
        async with self.db.engine.begin() as conn:
            found = (
                (
                    await conn.execute(
                        select(patients)
                        .where(and_(patients.c.tenant == tenant, patients.c.id == patient))
                        .with_for_update()
                    )
                )
                .mappings()
                .first()
            )
            if found is None or found["restricted"]:
                raise PatientLoginError("La ficha no está disponible.")
            await conn.execute(
                insert(principals).values(
                    tenant=tenant,
                    id=f"patient:{patient}:{secrets.token_hex(8)}",
                    kind=PATIENT,
                    roles=[],
                    subject_id=patient,
                    key_hash=digest(key),
                    created_at=utcnow(),
                    created_by=actor,
                    expires_at=utcnow() + timedelta(days=3),
                )
            )
            await conn.execute(
                update(patient_logins)
                .where(self.identity(patient_logins, tenant, patient))
                .values(recovery_hash=digest(key))
            )
            await self.audit.record_in(
                conn,
                tenant,
                actor,
                "patient.account.recovery",
                f"patient/{patient}",
                subject_id=patient,
            )
        return {"key": key}

    async def purge(self, *, dry_run: bool = False) -> int:
        async with self.db.engine.begin() as conn:
            query = patient_sessions.c.expires_at <= utcnow()
            rows = list(
                (await conn.execute(select(patient_sessions.c.key_hash).where(query))).scalars()
            )
            if not dry_run:
                await conn.execute(delete(patient_sessions).where(query))
            return len(rows)
