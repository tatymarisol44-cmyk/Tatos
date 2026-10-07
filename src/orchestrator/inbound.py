"""Incoming WhatsApp messages (ADR 0015, M4), privacy by design.

* **No message text is stored.** Each message is classified on arrival (`crisis.classify`)
  and only the outcome is kept.
* **Deduplicated** by the platform's message id: Meta retries failed deliveries for up to
  36 hours and does not guarantee single delivery.
* **STOP** records an opt-out under a pseudonymous key, SHA-256 of tenant and number.
  Phone numbers are a small space, so this is pseudonymous, not anonymous (it is still
  personal data); it is enough to suppress future sends without keeping the number.
* **Crisis and requests for a person** open an alert for the practice's staff, with the
  number so a professional can call back. The AI never answers these, and nothing is sent
  automatically: the wording of any automatic reply waits for the owner's lawyer (L3)."""

from __future__ import annotations

import hashlib
import logging
import re
import uuid
from typing import Any

from sqlalchemy import Column, DateTime, String, Table, and_, insert, select, update
from sqlalchemy.exc import IntegrityError

from orchestrator.crisis import classify
from orchestrator.db import Database, metadata, utcnow
from orchestrator.governance import AuditLog
from orchestrator.social import channel_accounts

log = logging.getLogger(__name__)

inbound_events = Table(
    "inbound_events",
    metadata,
    Column("network", String(16), primary_key=True),
    Column("message_id", String(128), primary_key=True),
    Column("tenant", String(64), nullable=False),
    Column("intent", String(16), nullable=False),
    Column("received_at", DateTime(timezone=True), nullable=False),
)

channel_alerts = Table(
    "channel_alerts",
    metadata,
    Column("tenant", String(64), primary_key=True),
    Column("alert_id", String(32), primary_key=True),
    Column("network", String(16), nullable=False),
    Column("account_id", String(32), nullable=False),
    Column("kind", String(16), nullable=False),  # crisis | human
    # The sender's address, so a professional can call back. Restricted, audited on read.
    Column("address", String(32), nullable=False),
    Column("status", String(16), nullable=False),  # open | resolved
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("resolved_by", String(128), nullable=True),
    Column("resolved_at", DateTime(timezone=True), nullable=True),
)

channel_optouts = Table(
    "channel_optouts",
    metadata,
    Column("tenant", String(64), primary_key=True),
    Column("network", String(16), primary_key=True),
    Column("address_key", String(64), primary_key=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

SYSTEM_ACTOR = "channel:whatsapp"


def normalize_address(address: str) -> str:
    return re.sub(r"\D", "", address)


def address_key(tenant: str, network: str, address: str) -> str:
    return hashlib.sha256(f"{tenant}:{network}:{normalize_address(address)}".encode()).hexdigest()


def _alert(row: Any) -> dict[str, Any]:
    return {c.name: getattr(row, c.name) for c in channel_alerts.c if c.name != "tenant"}


class InboundService:
    def __init__(self, db: Database, audit: AuditLog) -> None:
        self.db = db
        self.audit = audit

    async def account_for(self, network: str, external_id: str) -> tuple[str, str] | None:
        """The one tenant and account an incoming platform id belongs to; None if unknown
        or ambiguous (two tenants claiming the same id get nothing, not a guess)."""
        query = select(channel_accounts.c.tenant, channel_accounts.c.account_id).where(
            and_(
                channel_accounts.c.network == network,
                channel_accounts.c.external_id == external_id,
                channel_accounts.c.active.is_(True),
            )
        )
        async with self.db.engine.connect() as conn:
            rows = (await conn.execute(query)).all()
        if len(rows) != 1:
            if rows:
                log.warning("inbound %s: %d tenants claim the same account id", network, len(rows))
            return None
        return rows[0].tenant, rows[0].account_id

    async def handle_message(
        self, network: str, external_id: str, message_id: str, sender: str, text: str
    ) -> str:
        """Classify one message and act on it; returns the outcome (never the text)."""
        owner = await self.account_for(network, external_id)
        if owner is None:
            return "unknown_account"
        tenant, account_id = owner
        intent = classify(text)
        now = utcnow()
        try:
            async with self.db.engine.begin() as conn:
                await conn.execute(
                    insert(inbound_events).values(
                        network=network,
                        message_id=message_id,
                        tenant=tenant,
                        intent=intent,
                        received_at=now,
                    )
                )
                if intent == "stop":
                    key = address_key(tenant, network, sender)
                    exists = await conn.execute(
                        select(channel_optouts.c.address_key).where(
                            and_(
                                channel_optouts.c.tenant == tenant,
                                channel_optouts.c.network == network,
                                channel_optouts.c.address_key == key,
                            )
                        )
                    )
                    if exists.first() is None:
                        await conn.execute(
                            insert(channel_optouts).values(
                                tenant=tenant, network=network, address_key=key, created_at=now
                            )
                        )
                    await self.audit.record_in(
                        conn, tenant, SYSTEM_ACTOR, "channel.opt_out", f"channel_optout/{key[:12]}"
                    )
                elif intent in ("crisis", "human"):
                    alert_id = uuid.uuid4().hex[:12]
                    await conn.execute(
                        insert(channel_alerts).values(
                            tenant=tenant,
                            alert_id=alert_id,
                            network=network,
                            account_id=account_id,
                            kind=intent,
                            address=normalize_address(sender)[:32],
                            status="open",
                            created_at=now,
                        )
                    )
                    await self.audit.record_in(
                        conn,
                        tenant,
                        SYSTEM_ACTOR,
                        f"channel_alert.{intent}",
                        f"channel_alert/{alert_id}",
                    )
        except IntegrityError:
            return "duplicate"  # already handled: Meta delivered it again
        if intent == "crisis":
            # No personal data in logs: the alert id is enough for the on-duty person.
            log.warning("crisis alert opened for tenant %s", tenant)
        return intent

    async def is_opted_out(self, tenant: str, network: str, address: str) -> bool:
        query = select(channel_optouts.c.address_key).where(
            and_(
                channel_optouts.c.tenant == tenant,
                channel_optouts.c.network == network,
                channel_optouts.c.address_key == address_key(tenant, network, address),
            )
        )
        async with self.db.engine.connect() as conn:
            return (await conn.execute(query)).first() is not None

    async def alerts(
        self, tenant: str, actor: str, status: str | None = "open"
    ) -> list[dict[str, Any]]:
        query = select(channel_alerts).where(channel_alerts.c.tenant == tenant)
        if status is not None:
            query = query.where(channel_alerts.c.status == status)
        query = query.order_by(channel_alerts.c.created_at, channel_alerts.c.alert_id)
        async with self.db.engine.connect() as conn:
            rows = [_alert(r) for r in await conn.execute(query)]
        # Reading callers' numbers is itself audited.
        await self.audit.record(
            tenant, actor, "channel_alert.read", "channel_alerts", details={"count": len(rows)}
        )
        return rows

    async def resolve(self, tenant: str, alert_id: str, actor: str) -> bool:
        query = (
            update(channel_alerts)
            .where(
                and_(
                    channel_alerts.c.tenant == tenant,
                    channel_alerts.c.alert_id == alert_id,
                    channel_alerts.c.status == "open",
                )
            )
            .values(status="resolved", resolved_by=actor, resolved_at=utcnow())
        )
        async with self.db.engine.begin() as conn:
            done = (await conn.execute(query)).rowcount == 1
            if done:
                await self.audit.record_in(
                    conn, tenant, actor, "channel_alert.resolved", f"channel_alert/{alert_id}"
                )
        return done
