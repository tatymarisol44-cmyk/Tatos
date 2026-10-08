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
  automatically: the wording of any automatic reply waits for the owner's lawyer (L3).
* **A person may reply** to an alert by WhatsApp inside the 24-hour customer service window
  (counted from the alert, which is conservative), unless the number opted out. The reply's
  text is not stored; the audit records that a reply was sent, by whom and how it went."""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
import uuid
from datetime import timedelta
from typing import Any

import httpx
from sqlalchemy import Column, DateTime, Integer, String, Table, and_, insert, select, update
from sqlalchemy.exc import IntegrityError

from orchestrator.config import Settings
from orchestrator.crisis import classify
from orchestrator.db import Database, aware, metadata, utcnow
from orchestrator.governance import AuditLog
from orchestrator.publishers import PublishError, WhatsAppSender
from orchestrator.social import channel_accounts, resolve_secret

SERVICE_WINDOW = timedelta(hours=24)


class ReplyRefused(ValueError):
    """The reply cannot be sent (opted out, window closed, account disabled)."""


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
    # On-call escalation (ADR 0017): the level last notified, when the next level is due
    # (null: nothing more to do), and who took the alert, which stops the escalation.
    Column("escalation_level", Integer, nullable=False, default=0, server_default="0"),
    Column("next_escalation_at", DateTime(timezone=True), nullable=True),
    Column("acknowledged_by", String(128), nullable=True),
    Column("acknowledged_at", DateTime(timezone=True), nullable=True),
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


def address_key(key: bytes, tenant: str, network: str, address: str) -> str:
    """A keyed pseudonym of a phone number (HMAC-SHA256 with PSEUDONYM_KEY, which is never
    stored in the database). A plain hash would not do: there are only ~10^8 mobile
    numbers in Ecuador, so anyone holding a database copy could hash them all in minutes
    and learn who asked a mental-health practice to stop writing (re-identification test
    in tests/test_reidentification.py)."""
    message = f"{tenant}:{network}:{normalize_address(address)}".encode()
    return hmac.new(key, message, hashlib.sha256).hexdigest()


def _alert(row: Any) -> dict[str, Any]:
    return {c.name: getattr(row, c.name) for c in channel_alerts.c if c.name != "tenant"}


class InboundService:
    def __init__(
        self,
        db: Database,
        audit: AuditLog,
        settings: Settings,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.db = db
        self.audit = audit
        self.settings = settings
        self.transport = transport  # tests replace the network
        self.pseudonym_key = settings.pseudonym_secret()

    async def reply(self, tenant: str, alert_id: str, actor: str, body: str) -> dict[str, Any]:
        """A person's reply to an alert. Returns the outcome, never the text."""
        query = (
            select(channel_alerts, channel_accounts.c.external_id, channel_accounts.c.secret_ref)
            .join(
                channel_accounts,
                and_(
                    channel_accounts.c.tenant == channel_alerts.c.tenant,
                    channel_accounts.c.account_id == channel_alerts.c.account_id,
                    channel_accounts.c.active.is_(True),
                ),
                isouter=True,
            )
            .where(and_(channel_alerts.c.tenant == tenant, channel_alerts.c.alert_id == alert_id))
        )
        async with self.db.engine.connect() as conn:
            row = (await conn.execute(query)).first()
        if row is None:
            raise KeyError(alert_id)
        if row.network != "whatsapp":
            raise ReplyRefused(f"replies by {row.network} are not supported")
        if row.external_id is None:
            raise ReplyRefused("the account is disabled")
        if await self.is_opted_out(tenant, row.network, row.address):
            raise ReplyRefused("the person asked not to be contacted [WA-OPT-IN]")
        created = aware(row.created_at)
        if created is None or utcnow() - created > SERVICE_WINDOW:
            raise ReplyRefused(
                "outside the 24-hour window only an approved template may be sent [WA-WINDOW]"
            )

        token = resolve_secret(row.secret_ref)
        message_id: str | None = None
        if token is None:
            outcome = "dry_run"
        else:
            try:
                async with httpx.AsyncClient(
                    timeout=self.settings.publish_timeout_seconds, transport=self.transport
                ) as client:
                    message_id = await WhatsAppSender(
                        client, self.settings.whatsapp_graph_base
                    ).send_text(
                        phone_number_id=row.external_id, token=token, to=row.address, body=body
                    )
                outcome = "sent"
            except PublishError as exc:
                outcome = "failed"
                log.warning("whatsapp reply failed: %s", exc)
            except httpx.TransportError:
                outcome = "uncertain"  # may have been delivered: never retried automatically
        await self.audit.record(
            tenant,
            actor,
            f"channel_alert.reply_{outcome}",
            f"channel_alert/{alert_id}",
            details={"message_id": message_id},
        )
        return {"alert_id": alert_id, "outcome": outcome, "message_id": message_id}

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
                    key = address_key(self.pseudonym_key, tenant, network, sender)
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
                            escalation_level=0,
                            next_escalation_at=now,  # the on-call notifier takes it now
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
                channel_optouts.c.address_key
                == address_key(self.pseudonym_key, tenant, network, address),
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
            .values(
                status="resolved",
                resolved_by=actor,
                resolved_at=utcnow(),
                next_escalation_at=None,  # resolved: nobody else needs to be called
            )
        )
        async with self.db.engine.begin() as conn:
            done = (await conn.execute(query)).rowcount == 1
            if done:
                await self.audit.record_in(
                    conn, tenant, actor, "channel_alert.resolved", f"channel_alert/{alert_id}"
                )
        return done
