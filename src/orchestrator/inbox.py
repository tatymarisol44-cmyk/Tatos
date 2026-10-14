"""One inbox for WhatsApp, Instagram Direct and Facebook Messenger (ADR 0023).

* Every message, in and out, is kept **sealed** (sealing.py): the database never holds a
  readable text. Who wrote is kept sealed too, with a keyed pseudonym to find the
  conversation again (inbound.address_key).
* Reading a conversation is a clinical read: only the clinician role, each read audited
  (need to know, LOPDP Art. 10.e). The list shows a short preview to the same role only.
* The AI may **suggest** a reply; nothing it writes is sent until the professional sends it.
  It never drafts in a conversation flagged as a crisis or a request for a person, and its
  drafts pass the same deterministic checks as the WhatsApp assistant (no clinical or
  therapeutic advice, no links).
* Replies go out only inside the platform's 24-hour window opened by the person's last
  message, never to someone who wrote STOP, and record their outcome (sent, dry run,
  failed, uncertain) without the text in the audit.
* Retention: messages older than INBOX_RETENTION_DAYS are deleted by `agency retention`."""

from __future__ import annotations

import logging
import uuid
from datetime import timedelta
from typing import TYPE_CHECKING, Any, Literal

import httpx
from sqlalchemy import (
    Column,
    DateTime,
    Index,
    Integer,
    String,
    Table,
    Text,
    and_,
    delete,
    func,
    insert,
    select,
    update,
)

from orchestrator.config import Settings
from orchestrator.crm import patients
from orchestrator.db import Database, aware, metadata, utcnow
from orchestrator.governance import AuditLog
from orchestrator.inbound import address_key, deliver
from orchestrator.publishers import PublishError
from orchestrator.sealing import Sealer
from orchestrator.social import channel_accounts, resolve_secret

if TYPE_CHECKING:
    from orchestrator.inbound import InboundService
    from orchestrator.whatsapp_assistant import WhatsAppAssistant

log = logging.getLogger(__name__)

INBOX_NETWORKS = ("whatsapp", "instagram", "facebook")
SERVICE_WINDOW = timedelta(hours=24)
PREVIEW_CHARS = 80
MAX_BODY = 2000

inbox_conversations = Table(
    "inbox_conversations",
    metadata,
    Column("tenant", String(64), primary_key=True),
    Column("conversation_id", String(32), primary_key=True),
    Column("network", String(16), nullable=False),
    Column("account_id", String(32), nullable=False),
    # Keyed pseudonym of who writes (finds the conversation again) and the sealed address
    # itself (to reply): never a readable number or platform id.
    Column("address_key", String(64), nullable=False),
    Column("address_sealed", Text, nullable=False),
    Column("patient_id", String(64), nullable=True),
    Column("status", String(16), nullable=False),  # open | closed
    Column("attention", String(16), nullable=True),  # crisis | human: a person answers
    Column("unread", Integer, nullable=False, default=0, server_default="0"),
    Column("last_inbound_at", DateTime(timezone=True), nullable=True),
    Column("last_message_at", DateTime(timezone=True), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Index(
        "ux_inbox_conversations_who",
        "tenant",
        "network",
        "account_id",
        "address_key",
        unique=True,
    ),
)

inbox_messages = Table(
    "inbox_messages",
    metadata,
    Column("tenant", String(64), primary_key=True),
    Column("message_id", String(32), primary_key=True),
    Column("conversation_id", String(32), nullable=False),
    Column("direction", String(8), nullable=False),  # in | out
    # received | sent | dry_run | failed | uncertain | draft | discarded | used
    Column("status", String(16), nullable=False),
    Column("body_sealed", Text, nullable=False),
    Column("intent", String(16), nullable=True),  # inbound classification
    Column("author", String(128), nullable=True),  # who sent or drafted it (out)
    Column("external_id", String(128), nullable=True),  # the platform's message id
    Column("created_at", DateTime(timezone=True), nullable=False),
    Index("ix_inbox_messages_conversation", "tenant", "conversation_id", "created_at"),
)


class InboxError(ValueError):
    """A request the inbox refuses (window closed, opted out, crisis, unknown patient)."""


Outcome = Literal["sent", "dry_run", "failed", "uncertain"]


class InboxService:
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
        self.sealer = Sealer(self.pseudonym_key)
        self.inbound: InboundService | None = None  # set by the orchestrator
        self.assistant: WhatsAppAssistant | None = None  # set by the orchestrator

    # --- recording (called by the channels) ---------------------------------------------

    async def record_inbound(
        self,
        tenant: str,
        network: str,
        account_id: str,
        sender: str,
        text: str,
        intent: str,
        external_id: str,
    ) -> str:
        """Seal one incoming message into its conversation; returns the conversation id."""
        now = utcnow()
        conversation_id = await self._conversation(tenant, network, account_id, sender)
        attention = intent if intent in ("crisis", "human") else None
        values: dict[str, Any] = {
            "last_inbound_at": now,
            "last_message_at": now,
            "status": "open",
            "unread": inbox_conversations.c.unread + 1,
        }
        if attention is not None:
            values["attention"] = attention
        async with self.db.engine.begin() as conn:
            await conn.execute(
                insert(inbox_messages).values(
                    tenant=tenant,
                    message_id=uuid.uuid4().hex[:16],
                    conversation_id=conversation_id,
                    direction="in",
                    status="received",
                    body_sealed=self.sealer.seal(tenant, text[:MAX_BODY] or "[adjunto]"),
                    intent=intent,
                    external_id=external_id[:128],
                    created_at=now,
                )
            )
            await conn.execute(
                update(inbox_conversations)
                .where(self._key(tenant, conversation_id))
                .values(**values)
            )
        return conversation_id

    async def record_outbound(
        self,
        tenant: str,
        network: str,
        account_id: str,
        recipient: str,
        body: str,
        author: str,
        status: str,
        external_id: str | None = None,
    ) -> None:
        """An automatic reply sent by the channel (e.g. crisis containment): the
        professional sees in the conversation what the patient received."""
        conversation_id = await self._conversation(tenant, network, account_id, recipient)
        await self._append_out(tenant, conversation_id, body, author, status, external_id)

    # --- reading (clinician role, audited) ----------------------------------------------

    async def conversations(
        self, tenant: str, actor: str, network: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        query = select(inbox_conversations).where(inbox_conversations.c.tenant == tenant)
        if network:
            query = query.where(inbox_conversations.c.network == network)
        query = query.order_by(inbox_conversations.c.last_message_at.desc()).limit(limit)
        async with self.db.engine.connect() as conn:
            rows = (await conn.execute(query)).mappings().all()
            out = []
            for row in rows:
                last = (
                    await conn.execute(
                        select(inbox_messages.c.body_sealed, inbox_messages.c.direction)
                        .where(
                            and_(
                                inbox_messages.c.tenant == tenant,
                                inbox_messages.c.conversation_id == row["conversation_id"],
                                inbox_messages.c.status != "draft",
                                inbox_messages.c.status != "discarded",
                            )
                        )
                        .order_by(inbox_messages.c.created_at.desc())
                        .limit(1)
                    )
                ).first()
                preview = self.sealer.open(tenant, last.body_sealed) if last else ""
                out.append({**self._summary(row), "preview": preview[:PREVIEW_CHARS]})
        await self.audit.record(tenant, actor, "inbox.list", "inbox", details={"n": len(out)})
        return out

    async def conversation(self, tenant: str, conversation_id: str, actor: str) -> dict[str, Any]:
        row = await self._row(tenant, conversation_id)
        query = (
            select(inbox_messages)
            .where(
                and_(
                    inbox_messages.c.tenant == tenant,
                    inbox_messages.c.conversation_id == conversation_id,
                    inbox_messages.c.status != "discarded",
                )
            )
            .order_by(inbox_messages.c.created_at)
        )
        async with self.db.engine.begin() as conn:
            messages = (await conn.execute(query)).mappings().all()
            await conn.execute(
                update(inbox_conversations)
                .where(self._key(tenant, conversation_id))
                .values(unread=0)
            )
            await self.audit.record_in(
                conn, tenant, actor, "inbox.read", f"inbox_conversation/{conversation_id}"
            )
        return {
            **self._summary(row),
            "unread": 0,
            "window_open": self._window_open(row),
            "messages": [
                {
                    "message_id": m["message_id"],
                    "direction": m["direction"],
                    "status": m["status"],
                    "intent": m["intent"],
                    "author": m["author"],
                    "created_at": aware(m["created_at"]),
                    "text": self.sealer.open(tenant, m["body_sealed"]),
                }
                for m in messages
            ],
        }

    # --- acting (clinician role, audited) -----------------------------------------------

    async def draft(self, tenant: str, conversation_id: str, actor: str) -> dict[str, Any]:
        """A suggested reply by the AI, kept as a draft: never sent by itself."""
        row = await self._row(tenant, conversation_id)
        if row["attention"] in ("crisis", "human"):
            raise InboxError("a person answers this conversation, not the AI")
        if self.assistant is None:
            raise InboxError("the assistant is not available")
        last_in = await self._last_inbound_text(tenant, conversation_id)
        if last_in is None:
            raise InboxError("there is no message from the person to answer")
        phone = (
            self.sealer.open(tenant, row["address_sealed"])
            if row["network"] == "whatsapp"
            else None
        )
        text = await self.assistant.draft(tenant, last_in, phone=phone)
        if text is None:
            raise InboxError("no safe suggestion this time: write the reply yourself")
        message_id = await self._append_out(tenant, conversation_id, text, actor, "draft", None)
        await self.audit.record(
            tenant, actor, "inbox.draft", f"inbox_conversation/{conversation_id}"
        )
        return {"message_id": message_id, "text": text, "status": "draft"}

    async def reply(
        self,
        tenant: str,
        conversation_id: str,
        actor: str,
        body: str,
        draft_id: str | None = None,
    ) -> dict[str, Any]:
        """The professional's reply (written, or an edited or approved draft)."""
        body = body.strip()
        if not body:
            raise InboxError("the reply is empty")
        if len(body) > MAX_BODY:
            raise InboxError(f"the reply is longer than {MAX_BODY} characters")
        row = await self._row(tenant, conversation_id)
        if not self._window_open(row):
            raise InboxError(
                "the 24-hour window closed: the platform only allows approved templates"
            )
        recipient = self.sealer.open(tenant, row["address_sealed"])
        if self.inbound is not None and await self.inbound.is_opted_out(
            tenant, row["network"], recipient
        ):
            raise InboxError("the person asked not to be contacted")
        status, external_id = await self._deliver(
            tenant, row["network"], row["account_id"], recipient, body
        )
        message_id = await self._append_out(
            tenant, conversation_id, body, actor, status, external_id
        )
        if draft_id:
            async with self.db.engine.begin() as conn:
                await conn.execute(
                    update(inbox_messages)
                    .where(
                        and_(
                            inbox_messages.c.tenant == tenant,
                            inbox_messages.c.message_id == draft_id,
                            inbox_messages.c.conversation_id == conversation_id,
                            inbox_messages.c.status == "draft",
                        )
                    )
                    .values(status="used")
                )
        await self.audit.record(
            tenant,
            actor,
            f"inbox.reply_{status}",
            f"inbox_conversation/{conversation_id}",
            details={"message_id": external_id, "from_draft": bool(draft_id)},
        )
        return {"message_id": message_id, "status": status, "external_id": external_id}

    async def discard(self, tenant: str, conversation_id: str, draft_id: str, actor: str) -> bool:
        async with self.db.engine.begin() as conn:
            done = await conn.execute(
                update(inbox_messages)
                .where(
                    and_(
                        inbox_messages.c.tenant == tenant,
                        inbox_messages.c.conversation_id == conversation_id,
                        inbox_messages.c.message_id == draft_id,
                        inbox_messages.c.status == "draft",
                    )
                )
                .values(status="discarded")
            )
            if done.rowcount:
                await self.audit.record_in(
                    conn,
                    tenant,
                    actor,
                    "inbox.draft_discarded",
                    f"inbox_conversation/{conversation_id}",
                )
        return bool(done.rowcount)

    async def link_patient(
        self, tenant: str, conversation_id: str, patient_id: str, actor: str
    ) -> dict[str, Any]:
        await self._row(tenant, conversation_id)
        async with self.db.engine.begin() as conn:
            exists = (
                await conn.execute(
                    select(patients.c.id).where(
                        and_(patients.c.tenant == tenant, patients.c.id == patient_id)
                    )
                )
            ).first()
            if exists is None:
                raise InboxError("no such patient")
            await conn.execute(
                update(inbox_conversations)
                .where(self._key(tenant, conversation_id))
                .values(patient_id=patient_id)
            )
            await self.audit.record_in(
                conn,
                tenant,
                actor,
                "inbox.linked",
                f"inbox_conversation/{conversation_id}",
                subject_id=patient_id,
            )
        return {"conversation_id": conversation_id, "patient_id": patient_id}

    async def close(self, tenant: str, conversation_id: str, actor: str) -> None:
        """The professional handled it: closed, and a crisis or person flag cleared."""
        await self._row(tenant, conversation_id)
        async with self.db.engine.begin() as conn:
            await conn.execute(
                update(inbox_conversations)
                .where(self._key(tenant, conversation_id))
                .values(status="closed", attention=None, unread=0)
            )
            await self.audit.record_in(
                conn, tenant, actor, "inbox.closed", f"inbox_conversation/{conversation_id}"
            )

    async def for_patient(self, tenant: str, patient_id: str) -> list[dict[str, Any]]:
        """The open conversations linked to a patient, without their text (briefing.py)."""
        query = select(inbox_conversations).where(
            and_(
                inbox_conversations.c.tenant == tenant,
                inbox_conversations.c.patient_id == patient_id,
                inbox_conversations.c.status == "open",
            )
        )
        async with self.db.engine.connect() as conn:
            rows = (await conn.execute(query)).mappings().all()
        return [self._summary(row) for row in rows]

    # --- the patient's rights (subject_rights.py) ---------------------------------------

    async def export_subject(self, tenant: str, patient_id: str) -> list[dict[str, Any]]:
        """The conversations linked to the patient, opened, for an access request."""
        query = select(inbox_conversations).where(
            and_(
                inbox_conversations.c.tenant == tenant,
                inbox_conversations.c.patient_id == patient_id,
            )
        )
        async with self.db.engine.connect() as conn:
            rows = (await conn.execute(query)).mappings().all()
            out = []
            for row in rows:
                messages = (
                    await conn.execute(
                        select(inbox_messages)
                        .where(
                            and_(
                                inbox_messages.c.tenant == tenant,
                                inbox_messages.c.conversation_id == row["conversation_id"],
                                inbox_messages.c.status.not_in(("draft", "discarded")),
                            )
                        )
                        .order_by(inbox_messages.c.created_at)
                    )
                ).mappings()
                out.append(
                    {
                        "network": row["network"],
                        "messages": [
                            {
                                "direction": m["direction"],
                                "created_at": aware(m["created_at"]),
                                "text": self.sealer.open(tenant, m["body_sealed"]),
                            }
                            for m in messages
                        ],
                    }
                )
        return out

    async def erase_subject(self, tenant: str, patient_id: str) -> int:
        """Erasure: the conversations linked to the patient and their messages go."""
        linked = select(inbox_conversations.c.conversation_id).where(
            and_(
                inbox_conversations.c.tenant == tenant,
                inbox_conversations.c.patient_id == patient_id,
            )
        )
        async with self.db.engine.begin() as conn:
            await conn.execute(
                delete(inbox_messages).where(
                    and_(
                        inbox_messages.c.tenant == tenant,
                        inbox_messages.c.conversation_id.in_(linked),
                    )
                )
            )
            removed = await conn.execute(
                delete(inbox_conversations).where(
                    and_(
                        inbox_conversations.c.tenant == tenant,
                        inbox_conversations.c.patient_id == patient_id,
                    )
                )
            )
        return int(removed.rowcount or 0)

    async def purge(self, older_than: timedelta, dry_run: bool = False) -> int:
        """Retention: messages older than `older_than` go; so do conversations left empty."""
        cutoff = utcnow() - older_than
        old = inbox_messages.c.created_at < cutoff
        async with self.db.engine.begin() as conn:
            if dry_run:
                return int(
                    (
                        await conn.execute(
                            select(func.count()).select_from(inbox_messages).where(old)
                        )
                    ).scalar_one()
                )
            removed = (await conn.execute(delete(inbox_messages).where(old))).rowcount
            remaining = select(inbox_messages.c.conversation_id).where(
                and_(
                    inbox_messages.c.tenant == inbox_conversations.c.tenant,
                    inbox_messages.c.conversation_id == inbox_conversations.c.conversation_id,
                )
            )
            await conn.execute(
                delete(inbox_conversations).where(
                    and_(inbox_conversations.c.last_message_at < cutoff, ~remaining.exists())
                )
            )
        return int(removed or 0)

    # --- internals ----------------------------------------------------------------------

    @staticmethod
    def _key(tenant: str, conversation_id: str) -> Any:
        return and_(
            inbox_conversations.c.tenant == tenant,
            inbox_conversations.c.conversation_id == conversation_id,
        )

    @staticmethod
    def _summary(row: Any) -> dict[str, Any]:
        return {
            "conversation_id": row["conversation_id"],
            "network": row["network"],
            "patient_id": row["patient_id"],
            "status": row["status"],
            "attention": row["attention"],
            "unread": row["unread"],
            "last_message_at": aware(row["last_message_at"]),
        }

    @staticmethod
    def _window_open(row: Any) -> bool:
        last = aware(row["last_inbound_at"])
        return last is not None and utcnow() - last <= SERVICE_WINDOW

    async def _row(self, tenant: str, conversation_id: str) -> Any:
        async with self.db.engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        select(inbox_conversations).where(self._key(tenant, conversation_id))
                    )
                )
                .mappings()
                .first()
            )
        if row is None:
            raise KeyError(conversation_id)
        return row

    async def _conversation(self, tenant: str, network: str, account_id: str, who: str) -> str:
        key = address_key(self.pseudonym_key, tenant, network, who)
        where = and_(
            inbox_conversations.c.tenant == tenant,
            inbox_conversations.c.network == network,
            inbox_conversations.c.account_id == account_id,
            inbox_conversations.c.address_key == key,
        )
        async with self.db.engine.begin() as conn:
            found = (
                await conn.execute(select(inbox_conversations.c.conversation_id).where(where))
            ).first()
            if found is not None:
                return str(found.conversation_id)
            conversation_id = uuid.uuid4().hex[:16]
            now = utcnow()
            await conn.execute(
                insert(inbox_conversations).values(
                    tenant=tenant,
                    conversation_id=conversation_id,
                    network=network,
                    account_id=account_id,
                    address_key=key,
                    address_sealed=self.sealer.seal(tenant, who),
                    status="open",
                    unread=0,
                    last_message_at=now,
                    created_at=now,
                )
            )
        return conversation_id

    async def _append_out(
        self,
        tenant: str,
        conversation_id: str,
        body: str,
        author: str,
        status: str,
        external_id: str | None,
    ) -> str:
        message_id = uuid.uuid4().hex[:16]
        now = utcnow()
        async with self.db.engine.begin() as conn:
            await conn.execute(
                insert(inbox_messages).values(
                    tenant=tenant,
                    message_id=message_id,
                    conversation_id=conversation_id,
                    direction="out",
                    status=status,
                    body_sealed=self.sealer.seal(tenant, body),
                    author=author,
                    external_id=external_id,
                    created_at=now,
                )
            )
            if status != "draft":
                await conn.execute(
                    update(inbox_conversations)
                    .where(self._key(tenant, conversation_id))
                    .values(last_message_at=now)
                )
        return message_id

    async def _last_inbound_text(self, tenant: str, conversation_id: str) -> str | None:
        query = (
            select(inbox_messages.c.body_sealed)
            .where(
                and_(
                    inbox_messages.c.tenant == tenant,
                    inbox_messages.c.conversation_id == conversation_id,
                    inbox_messages.c.direction == "in",
                )
            )
            .order_by(inbox_messages.c.created_at.desc())
            .limit(1)
        )
        async with self.db.engine.connect() as conn:
            row = (await conn.execute(query)).first()
        return self.sealer.open(tenant, row.body_sealed) if row else None

    async def _deliver(
        self, tenant: str, network: str, account_id: str, to: str, body: str
    ) -> tuple[Outcome, str | None]:
        query = select(channel_accounts.c.external_id, channel_accounts.c.secret_ref).where(
            and_(
                channel_accounts.c.tenant == tenant,
                channel_accounts.c.account_id == account_id,
                channel_accounts.c.active.is_(True),
            )
        )
        async with self.db.engine.connect() as conn:
            account = (await conn.execute(query)).first()
        if account is None:
            raise InboxError("the account is disabled")
        token = resolve_secret(account.secret_ref)
        if token is None:
            return "dry_run", None
        try:
            message_id = await deliver(
                self.settings, self.transport, network, account.external_id, token, to, body
            )
        except PublishError as exc:
            log.warning("inbox reply failed: %s", exc)
            return "failed", None
        except httpx.TransportError:
            return "uncertain", None  # may have been delivered: never retried automatically
        return "sent", message_id
