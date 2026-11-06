"""Multi-issuer tax architecture with an explicitly non-fiscal private prototype.

There is no SRI transport, official XSD, certificate loader, signature or live switch.
The XML uses our own namespace and cannot be presented as an authorised invoice.
Real adapters must implement the port only after schemas, signing and credentials
are independently verified in SRI's test environment.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import timedelta
from typing import Any, Literal, Protocol
from xml.etree.ElementTree import Element, SubElement, tostring

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator
from sqlalchemy import (
    JSON,
    BigInteger,
    Column,
    DateTime,
    Index,
    Integer,
    String,
    Table,
    Text,
    UniqueConstraint,
    delete,
    insert,
    select,
    update,
)
from sqlalchemy.exc import IntegrityError

from orchestrator.auth import Principal
from orchestrator.billing_store import orders
from orchestrator.db import Database, aware, metadata, utcnow
from orchestrator.governance import AuditLog

issuers = Table(
    "tax_trial_issuers",
    metadata,
    Column("tenant", String(64), primary_key=True),
    Column("id", String(32), primary_key=True),
    Column("ruc", String(13), nullable=False),
    Column("version", Integer, nullable=False),
    Column("profile", JSON, nullable=False),
    Column("next_sequence", BigInteger, nullable=False),
    UniqueConstraint("tenant", "ruc", name="uq_tax_trial_ruc"),
)
documents = Table(
    "tax_trial_documents",
    metadata,
    Column("tenant", String(64), primary_key=True),
    Column("id", String(32), primary_key=True),
    Column("issuer_id", String(32), nullable=False),
    Column("request_id", String(64), nullable=False),
    Column("request_hash", String(64), nullable=False),
    Column("order_id", String(32), nullable=False),
    Column("sequence", BigInteger, nullable=False),
    Column("revision", Integer, nullable=False),
    Column("attempts", Integer, nullable=False),
    Column("retry_at", DateTime(timezone=True), nullable=True),
    Column("payload", JSON, nullable=False),
    Column("xml", Text, nullable=False),
    Column("state", String(32), nullable=False),
    Column("outcome", JSON, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("tenant", "request_id", name="uq_tax_trial_request"),
    UniqueConstraint("tenant", "issuer_id", "sequence", name="uq_tax_trial_sequence"),
    UniqueConstraint("tenant", "issuer_id", "order_id", name="uq_tax_trial_order"),
    Index("ix_tax_trial_created", "tenant", "created_at"),
)


class TaxTrialError(ValueError):
    pass


class IssuerProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    ruc: str = Field(pattern=r"^[0-9]{13}$")
    legal_name: str = Field(min_length=1, max_length=200)
    trade_name: str = Field(min_length=1, max_length=200)
    address: str = Field(min_length=1, max_length=300)
    establishment: str = Field(pattern=r"^[0-9]{3}$")
    emission_point: str = Field(pattern=r"^[0-9]{3}$")
    tax_regime: str = Field(min_length=1, max_length=200)
    signing_reference: str = Field(default="", pattern=r"^$|^vault:[\w/.-]{1,100}$")
    # This inert reference is not a P12, passphrase, token or evidence of a signature.


class TrialRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    issuer_id: str = Field(pattern=r"^[0-9a-f]{16}$")
    request_id: str = Field(pattern=r"^[\w-]{1,64}$")
    order_id: str = Field(pattern=r"^[0-9a-f]{1,32}$")
    net_cents: StrictInt = Field(ge=0, le=1000000000)
    tax_cents: StrictInt = Field(ge=0, le=1000000000)
    tax_basis_reference: str = Field(min_length=1, max_length=300)
    scenario: Literal["accept", "reject", "transient"] = "accept"
    mode: Literal["non_fiscal_simulation"] = "non_fiscal_simulation"

    @model_validator(mode="after")
    def nonzero(self) -> TrialRequest:
        if self.net_cents + self.tax_cents <= 0:
            raise ValueError("the demonstration total must be positive")
        return self


class TaxProvider(Protocol):
    """Deterministic simulation port only; must never do network I/O under a DB lock."""

    def process(self, payload: dict[str, Any], attempt: int) -> dict[str, Any]: ...


class FiscalGateway(Protocol):
    """Unimplemented future queue boundary, separate from simulation transactions.

    Verified signing/XSD preparation precedes submission. Persist request identity
    before I/O; on uncertainty query this identity instead of allocating a new series.
    An implementation must validate issuer/environment and the provider's response.
    """

    async def submit(
        self, issuer_ref: str, signed_xml: bytes, idempotency_key: str
    ) -> dict[str, Any]: ...

    async def status(self, issuer_ref: str, idempotency_key: str) -> dict[str, Any]: ...


class NonFiscalProvider:
    def process(self, payload: dict[str, Any], attempt: int) -> dict[str, Any]:
        scenario = payload["scenario"]
        state = (
            "retry_wait"
            if scenario == "transient" and attempt == 1
            else "simulated_rejected"
            if scenario == "reject"
            else "simulated_accepted"
        )
        return {
            "state": state,
            "provider": "local_non_fiscal",
            "sri_authorised": False,
            "signed": False,
            "official_xsd_validated": False,
            "message": "ENSAYO LOCAL SIN VALIDEZ TRIBUTARIA. No se envió al SRI.",
        }


def trial_xml(payload: dict[str, Any]) -> str:
    root = Element(
        "ensayo", {"xmlns": "urn:tatos:non-fiscal-trial:1", "validezTributaria": "ninguna"}
    )
    SubElement(root, "advertencia").text = "SIMULACIÓN PRIVADA. NO ES FACTURA NI XML OFICIAL SRI."
    for key in ("id", "sequence", "currency", "net_cents", "tax_cents", "total_cents"):
        SubElement(root, key).text = str(payload[key])
    profile = SubElement(root, "emisorDePrueba")
    for key, value in payload["issuer"].items():
        if key != "signing_reference":
            SubElement(profile, key).text = str(value)
    return tostring(root, encoding="unicode")


class TaxTrials:
    def __init__(self, db: Database, audit: AuditLog, app_env: str) -> None:
        self.db, self.audit, self.app_env = db, audit, app_env
        self.provider: TaxProvider = NonFiscalProvider()

    def private(self) -> None:
        if self.app_env == "prod":
            raise TaxTrialError("the non-fiscal prototype is disabled in production")

    async def profiles(self, tenant: str) -> dict[str, Any]:
        self.private()
        async with self.db.engine.connect() as conn:
            rows = (
                (await conn.execute(select(issuers).where(issuers.c.tenant == tenant)))
                .mappings()
                .all()
            )
        return {
            "mode": "non_fiscal_simulation",
            "issuers": [dict(r) for r in rows],
            "blockers": [
                "official_XSD_not_verified",
                "signer_and_test_certificate_not_configured",
                "SRI_test_transport_not_implemented",
                "RIDE_not_implemented",
            ],
            "sri_authorised": False,
        }

    async def configure(
        self, p: Principal, profile: IssuerProfile, identifier: str | None, version: int
    ) -> dict[str, Any]:
        self.private()
        identifier = identifier or uuid.uuid4().hex[:16]
        try:
            async with self.db.engine.begin() as conn:
                row = (
                    (
                        await conn.execute(
                            select(issuers)
                            .where(issuers.c.tenant == p.tenant, issuers.c.id == identifier)
                            .with_for_update()
                        )
                    )
                    .mappings()
                    .first()
                )
                if row is None and version != 0:
                    raise KeyError(identifier)
                if row is not None and row["version"] != version:
                    raise TaxTrialError("issuer changed; reload before saving")
                if row is not None and row["ruc"] != profile.ruc:
                    raise TaxTrialError("register a separate issuer instead of changing its RUC")
                values = {
                    "ruc": profile.ruc,
                    "version": version + 1,
                    "profile": profile.model_dump(),
                }
                if row is None:
                    await conn.execute(
                        insert(issuers).values(
                            tenant=p.tenant, id=identifier, next_sequence=1, **values
                        )
                    )
                else:
                    await conn.execute(
                        update(issuers)
                        .where(issuers.c.tenant == p.tenant, issuers.c.id == identifier)
                        .values(**values)
                    )
                await self.audit.record_in(
                    conn,
                    p.tenant,
                    p.id,
                    "tax_trial.issuer.updated",
                    f"tax_trial_issuer/{identifier}",
                )
        except IntegrityError as exc:
            raise TaxTrialError("issuer already registered or changed concurrently") from exc
        return {"id": identifier, "version": version + 1, "mode": "non_fiscal_simulation"}

    async def listing(self, tenant: str) -> list[dict[str, Any]]:
        self.private()
        async with self.db.engine.connect() as conn:
            rows = (
                (
                    await conn.execute(
                        select(documents)
                        .where(documents.c.tenant == tenant)
                        .order_by(documents.c.created_at.desc())
                        .limit(100)
                    )
                )
                .mappings()
                .all()
            )
        return [self.public(r) for r in rows]

    @staticmethod
    def public(row: Any) -> dict[str, Any]:
        result = {
            k: row[k]
            for k in (
                "id",
                "issuer_id",
                "order_id",
                "sequence",
                "revision",
                "attempts",
                "state",
                "outcome",
            )
        }
        result["created_at"], result["retry_at"] = aware(row["created_at"]), aware(row["retry_at"])
        result["mode"] = "non_fiscal_simulation"
        return result

    async def get(self, p: Principal, identifier: str) -> dict[str, Any]:
        self.private()
        async with self.db.engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        select(documents).where(
                            documents.c.tenant == p.tenant, documents.c.id == identifier
                        )
                    )
                )
                .mappings()
                .first()
            )
        if row is None:
            raise KeyError(identifier)
        await self.audit.record(
            p.tenant, p.id, "tax_trial.document.read", f"tax_trial_document/{identifier}"
        )
        return {
            **self.public(row),
            "payload": row["payload"],
            "xml": row["xml"],
            "xml_sha256": hashlib.sha256(row["xml"].encode()).hexdigest(),
        }

    async def create(self, p: Principal, request: TrialRequest) -> dict[str, Any]:
        self.private()
        fingerprint = hashlib.sha256(request.model_dump_json().encode()).hexdigest()
        try:
            async with self.db.engine.begin() as conn:
                # Lock issuer first: its private sequence and every document advance atomically.
                issuer = (
                    (
                        await conn.execute(
                            select(issuers)
                            .where(issuers.c.tenant == p.tenant, issuers.c.id == request.issuer_id)
                            .with_for_update()
                        )
                    )
                    .mappings()
                    .first()
                )
                if issuer is None:
                    raise KeyError(request.issuer_id)
                existing = (
                    (
                        await conn.execute(
                            select(documents).where(
                                documents.c.tenant == p.tenant,
                                documents.c.request_id == request.request_id,
                            )
                        )
                    )
                    .mappings()
                    .first()
                )
                if existing:
                    if existing["request_hash"] != fingerprint:
                        raise TaxTrialError("request id already used with a different payload")
                    return self.public(existing)
                order = (
                    (
                        await conn.execute(
                            select(orders).where(
                                orders.c.tenant == p.tenant, orders.c.invoice_id == request.order_id
                            )
                        )
                    )
                    .mappings()
                    .first()
                )
                if order is None or order["kind"] != "appointment":
                    raise KeyError(request.order_id)
                if (
                    request.net_cents + request.tax_cents != order["amount_cents"]
                    or order["currency"] != "USD"
                ):
                    raise TaxTrialError(
                        "trial totals must match the USD obligation; no VAT is inferred"
                    )
                sequence = issuer["next_sequence"]
                if sequence > 999999999:
                    raise TaxTrialError("private sequence exhausted")
                identifier = uuid.uuid4().hex[:16]
                payload = {
                    "id": "ENSAYO-" + identifier,
                    "issuer": issuer["profile"],
                    "issuer_version": issuer["version"],
                    "sequence": sequence,
                    "currency": "USD",
                    "net_cents": request.net_cents,
                    "tax_cents": request.tax_cents,
                    "total_cents": order["amount_cents"],
                    "tax_basis_reference": request.tax_basis_reference,
                    "scenario": request.scenario,
                    "not_a_fiscal_invoice": True,
                }
                outcome = self.provider.process(payload, 1)
                await conn.execute(
                    insert(documents).values(
                        tenant=p.tenant,
                        id=identifier,
                        issuer_id=request.issuer_id,
                        request_id=request.request_id,
                        request_hash=fingerprint,
                        order_id=request.order_id,
                        sequence=sequence,
                        revision=1,
                        attempts=1,
                        retry_at=utcnow() + timedelta(minutes=1)
                        if outcome["state"] == "retry_wait"
                        else None,
                        payload=payload,
                        xml=trial_xml(payload),
                        state=outcome["state"],
                        outcome=outcome,
                        created_at=utcnow(),
                    )
                )
                await conn.execute(
                    update(issuers)
                    .where(issuers.c.tenant == p.tenant, issuers.c.id == request.issuer_id)
                    .values(next_sequence=sequence + 1)
                )
                await self.audit.record_in(
                    conn,
                    p.tenant,
                    p.id,
                    "tax_trial.document.simulated",
                    f"tax_trial_document/{identifier}",
                    details={
                        "issuer_id": request.issuer_id,
                        "sequence": sequence,
                        "state": outcome["state"],
                        "sri_authorised": False,
                    },
                )
        except IntegrityError as exc:
            raise TaxTrialError(
                "an order, request or sequence is already registered; reload"
            ) from exc
        return await self.get(p, identifier)

    async def retry(self, p: Principal, identifier: str, revision: int) -> dict[str, Any]:
        self.private()
        async with self.db.engine.begin() as conn:
            row = (
                (
                    await conn.execute(
                        select(documents)
                        .where(documents.c.tenant == p.tenant, documents.c.id == identifier)
                        .with_for_update()
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                raise KeyError(identifier)
            due = aware(row["retry_at"])
            if (
                row["revision"] != revision
                or row["state"] != "retry_wait"
                or row["attempts"] >= 3
                or due is None
                or due > utcnow()
            ):
                raise TaxTrialError(
                    "retry not eligible: revision, due time, state or attempt limit"
                )
            attempt = row["attempts"] + 1
            outcome = self.provider.process(row["payload"], attempt)
            await conn.execute(
                update(documents)
                .where(documents.c.tenant == p.tenant, documents.c.id == identifier)
                .values(
                    revision=revision + 1,
                    attempts=attempt,
                    retry_at=utcnow() + timedelta(minutes=2**attempt)
                    if outcome["state"] == "retry_wait"
                    else None,
                    state=outcome["state"],
                    outcome=outcome,
                )
            )
            await self.audit.record_in(
                conn,
                p.tenant,
                p.id,
                "tax_trial.document.retried",
                f"tax_trial_document/{identifier}",
                details={"attempt": attempt, "state": outcome["state"]},
            )
        return await self.get(p, identifier)

    async def export_subject(self, tenant: str, patient_id: str) -> list[dict[str, Any]]:
        # No issuer vault references, bank references or clinical details in the export.
        async with self.db.engine.connect() as conn:
            rows = (
                (
                    await conn.execute(
                        select(documents)
                        .join(
                            orders,
                            (orders.c.tenant == documents.c.tenant)
                            & (orders.c.invoice_id == documents.c.order_id),
                        )
                        .where(documents.c.tenant == tenant, orders.c.patient_id == patient_id)
                    )
                )
                .mappings()
                .all()
            )
        return [self.public(row) for row in rows]

    async def erase_subject(self, tenant: str, patient_id: str) -> int:
        # These are optional local trials, not legally retained fiscal documents.
        ids = select(orders.c.invoice_id).where(
            orders.c.tenant == tenant, orders.c.patient_id == patient_id
        )
        async with self.db.engine.begin() as conn:
            result = await conn.execute(
                delete(documents).where(documents.c.tenant == tenant, documents.c.order_id.in_(ids))
            )
        return int(result.rowcount or 0)
