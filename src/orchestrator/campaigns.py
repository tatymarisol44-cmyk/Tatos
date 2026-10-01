"""Loyalty campaigns with a holdout group, compliance checks and human approval.

    insight segment -> draft (copy checked against the pack) -> approve (a person, for
    this exact version of the text) -> queue -> deliver (outbox) -> measure lift

Campaign states: draft | pending_approval | approved | queued | sending | completed |
partial_failed | cancelled. Every edit bumps `version`; an approval covers one version
(and the hash of its text), so a late edit can neither reopen an approved campaign nor
change what is sent (audit finding A13).

Delivery is an outbox (A12): queueing marks each treatment recipient `queued`; a worker
claims recipients one at a time (a conditional UPDATE, safe across replicas) and records
the outcome of each. Right before every delivery it re-checks the campaign (cancelled?),
the `marketing` consent and the contact record (A15), and reserves room under the
patient's monthly cap with an atomic UPDATE on a counter shared by all campaigns (A14).

Channels, in order:
1. In the app: the offer appears in the patient's portal (`/v1/me`). It takes no room
   under the cap: the patient opens it when they choose.
2. Telegram, as a fallback: if the patient has not seen the offer after
   CAMPAIGN_FALLBACK_HOURS (or has no app access), it is sent as a message.

Outcomes per recipient: in_app, seen, sent, dry_run (no bot token: recorded, not
delivered), failed (certainly not delivered: may be retried), uncertain (the request
left and no answer came back: never re-sent blindly, an operator decides), skipped_*
(no consent, no channel, cap), held_out (control arm), cancelled.

Opting out (A11): every message must tell the recipient how to stop (a reply
instruction with the word STOP or BAJA, not a substring such as "nonstop"), and a STOP
reply to the bot withdraws the marketing consent at once (`handle_inbound`).

- The control arm is chosen by a hash of (campaign, subject), so the split is
  reproducible and does not depend on the order recipients were listed in.
- Lift is the difference in booking rate between arms within the conversion window,
  with a two-proportion z-test. With small arms the result is labelled as inconclusive
  instead of being reported as an effect.
- Messages carry no clinical details (checked) and are personalised only with the first
  name."""

from __future__ import annotations

import hashlib
import logging
import math
import re
import uuid
from datetime import datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    Integer,
    String,
    Table,
    Text,
    and_,
    delete,
    func,
    insert,
    or_,
    select,
    update,
)
from sqlalchemy.exc import IntegrityError

from orchestrator.auth import PATIENT, principals
from orchestrator.config import Settings
from orchestrator.crm import CrmService, appointments, patients
from orchestrator.db import Database, aware, metadata, utcnow
from orchestrator.governance import AuditLog, ConsentRegistry, Purpose
from orchestrator.insights import InsightsService
from orchestrator.llm import LLMClient
from orchestrator.packs import pack_for
from orchestrator.risk import CopyCheck, check_copy, fold

log = logging.getLogger(__name__)

KINDS = ("recall", "reactivation", "pending_treatment", "referral", "birthday", "education")
CHANNELS = ("telegram",)
_PLACEHOLDER = re.compile(r"\{(\w+)\}")
ALLOWED_PLACEHOLDERS = {"first_name"}
MIN_ARM_SIZE = 30

# A real opt-out instruction: a reply verb followed (closely) by the keyword as a word.
_OPT_OUT = re.compile(
    r"\b(respond\w*|reply|escrib\w*|envi\w*|text|send|contest\w*)\b[^.!?\n]{0,40}?\b(stop|baja)\b",
    re.IGNORECASE,
)
# Inbound replies that withdraw the marketing consent (the whole message, folded).
STOP_WORDS = {"stop", "baja", "parar", "cancelar", "unsubscribe", "darme de baja"}
OPT_OUT_SENTENCE = "Responde STOP para no recibir más mensajes."
STOP_CONFIRMATION = (
    "Listo: no recibirás más mensajes promocionales. Tus citas y recordatorios no cambian."
)

# Delivery rows of erased patients: outcome kept, identity and timestamps gone.
ANON_PREFIX = "anon:"
EDITABLE = ("draft", "pending_approval")
CANCELLABLE = ("draft", "pending_approval", "approved", "queued", "sending")
SENT_STATES = ("sending", "completed", "partial_failed")
# Recipients the treatment arm actually reached (for results and the patient's inbox).
REACHED = ("in_app", "seen", "sent", "dry_run")
_FINAL_FAILURES = ("failed", "uncertain")

COPYWRITER_PROMPT = """You are the COPYWRITER of a small business. Write one short, warm
message (max 320 characters) for a {kind} campaign sent to customers in the "{segment}"
segment. Use {{first_name}} for the customer's first name and no other placeholder. Never
mention health conditions, treatments, diagnoses or prices, never promise results, and
always end with: "{opt_out}". Language: {language}. Reply with the message only."""

campaigns = Table(
    "campaigns",
    metadata,
    Column("tenant", String(64), primary_key=True),
    Column("id", String(64), primary_key=True),
    Column("name", String(200), nullable=False),
    Column("kind", String(32), nullable=False),
    Column("segment", String(32), nullable=False),
    Column("channel", String(16), nullable=False),
    Column("template", Text, nullable=False),
    Column("status", String(24), nullable=False),
    Column("version", Integer, nullable=False, default=1),
    Column("holdout_pct", Integer, nullable=False),
    Column("compliance", JSON, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("created_by", String(128), nullable=False),
    Column("approved_by", String(128), nullable=True),
    Column("approved_at", DateTime(timezone=True), nullable=True),
    # What the approval covers: this version, this text.
    Column("approved_version", Integer, nullable=True),
    Column("approved_hash", String(64), nullable=True),
    Column("sent_at", DateTime(timezone=True), nullable=True),  # queued for delivery
    Column("completed_at", DateTime(timezone=True), nullable=True),
)

recipients = Table(
    "campaign_recipients",
    metadata,
    Column("tenant", String(64), primary_key=True),
    Column("campaign_id", String(64), primary_key=True),
    Column("patient_id", String(64), primary_key=True),
    Column("arm", String(16), nullable=False),  # treatment|control
    Column("status", String(24), nullable=False),  # see the module docstring
    Column("channel", String(16), nullable=True),  # app|telegram
    Column("attempts", Integer, nullable=False, default=0),
    Column("claimed_at", DateTime(timezone=True), nullable=True),
    Column("sent_at", DateTime(timezone=True), nullable=True),
    Column("seen_at", DateTime(timezone=True), nullable=True),
    Column("fallback_due_at", DateTime(timezone=True), nullable=True),
    Column("error", String(200), nullable=True),
)

# Messages pushed to each patient per calendar month (UTC), across all campaigns. The
# cap is enforced by a conditional UPDATE on this row: two workers cannot both take the
# last slot.
contact_budget = Table(
    "contact_budget",
    metadata,
    Column("tenant", String(64), primary_key=True),
    Column("patient_id", String(64), primary_key=True),
    Column("period", String(7), primary_key=True),  # YYYY-MM
    Column("used", Integer, nullable=False),
)


class CampaignError(ValueError):
    pass


class DeliveryUncertain(RuntimeError):
    """The request reached the network and no definite answer came back."""


class _NotSent(Exception):
    """An error raised before anything left: the recipient's claim can be undone."""


def arm_for(campaign_id: str, patient_id: str, holdout_pct: int) -> str:
    digest = hashlib.sha256(f"{campaign_id}:{patient_id}".encode()).digest()
    return "control" if int.from_bytes(digest[:4], "big") % 100 < holdout_pct else "treatment"


def placeholders_ok(template: str) -> list[str]:
    return sorted({p for p in _PLACEHOLDER.findall(template) if p not in ALLOWED_PLACEHOLDERS})


def has_opt_out(text: str) -> bool:
    return bool(_OPT_OUT.search(text))


def is_stop(text: str) -> bool:
    return fold(text).strip().strip(".!¡ ").lower() in STOP_WORDS


def text_hash(template: str) -> str:
    return hashlib.sha256(template.encode()).hexdigest()


def render(template: str, display_name: str) -> str:
    first = (display_name or "").split()[0] if display_name else ""
    return template.replace("{first_name}", first)


def two_proportion_p(conv_a: int, n_a: int, conv_b: int, n_b: int) -> float | None:
    """Two-sided p-value of H0: both arms convert at the same rate (normal approx.)."""
    if n_a == 0 or n_b == 0:
        return None
    pooled = (conv_a + conv_b) / (n_a + n_b)
    se = math.sqrt(pooled * (1 - pooled) * (1 / n_a + 1 / n_b))
    if se == 0:
        return None
    z = (conv_a / n_a - conv_b / n_b) / se
    return math.erfc(abs(z) / math.sqrt(2))


class TelegramChannel:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._http: httpx.AsyncClient | None = None

    @property
    def live(self) -> bool:
        return self.settings.telegram_bot_token is not None

    async def send(self, chat_id: str, text: str) -> str:
        """'sent' or 'dry_run'. Raises RuntimeError when the message was certainly not
        delivered, DeliveryUncertain when it may have been."""
        if self.settings.telegram_bot_token is None:
            return "dry_run"
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=10.0)
        token = self.settings.telegram_bot_token.get_secret_value()
        try:
            resp = await self._http.post(
                f"{self.settings.telegram_api_base}/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": text},
            )
        except (httpx.ConnectError, httpx.ConnectTimeout):
            # Never chain the exception: its text contains the URL, and so the bot token.
            raise RuntimeError("telegram unreachable") from None
        except httpx.TransportError:
            raise DeliveryUncertain("telegram did not answer") from None
        if resp.status_code >= 500:
            raise DeliveryUncertain(f"telegram HTTP {resp.status_code}")
        if resp.status_code != 200 or not resp.json().get("ok"):
            raise RuntimeError(f"telegram HTTP {resp.status_code}")
        return "sent"

    async def close(self) -> None:
        if self._http is not None:
            await self._http.aclose()


def _campaign(row: Any) -> dict[str, Any]:
    out = dict(row)
    for key in ("created_at", "approved_at", "sent_at", "completed_at"):
        value = aware(out.get(key))
        out[key] = value.isoformat() if value else None
    return out


def _period(now: datetime) -> str:
    return now.strftime("%Y-%m")


class CampaignService:
    def __init__(
        self,
        db: Database,
        audit: AuditLog,
        consents: ConsentRegistry,
        crm: CrmService,
        insights: InsightsService,
        llm: LLMClient,
        settings: Settings,
    ) -> None:
        self.db = db
        self.audit = audit
        self.consents = consents
        self.crm = crm
        self.insights = insights
        self.llm = llm
        self.settings = settings
        self.telegram = TelegramChannel(settings)

    async def close(self) -> None:
        await self.telegram.close()

    def _check(self, tenant: str, template: str) -> CopyCheck:
        check = check_copy(template, pack_for(self.settings, tenant))
        bad = placeholders_ok(template)
        if bad:
            check.ok = False
            check.violations.extend(f"placeholder:{p}" for p in bad)
        if not has_opt_out(template):
            check.ok = False
            check.violations.append("missing_opt_out:reply STOP")
        return check

    async def draft_copy(self, kind: str, segment: str, language: str) -> str:
        result = await self.llm.complete(
            [
                {
                    "role": "system",
                    "content": COPYWRITER_PROMPT.format(
                        kind=kind, segment=segment, language=language, opt_out=OPT_OUT_SENTENCE
                    ),
                },
                {"role": "user", "content": f"Campaign: {kind}. Segment: {segment}."},
            ],
            model=self.settings.llm_model,
            temperature=0.4,
            max_tokens=200,
        )
        text = result.text.strip()
        # The opt-out line is a legal requirement, not a matter of style.
        return text if has_opt_out(text) else f"{text} {OPT_OUT_SENTENCE}"

    async def create(
        self,
        tenant: str,
        *,
        name: str,
        kind: str,
        segment: str,
        channel: str,
        actor: str,
        template: str | None = None,
        holdout_pct: int | None = None,
        language: str = "es",
    ) -> dict[str, Any]:
        if kind not in KINDS:
            raise CampaignError(f"unknown kind {kind!r}; one of {KINDS}")
        if channel not in CHANNELS:
            raise CampaignError(f"unsupported channel {channel!r}; one of {CHANNELS}")
        members = await self.insights.segment_members(tenant, segment)
        text = template or await self.draft_copy(kind, segment, language)
        check = self._check(tenant, text)
        cid = uuid.uuid4().hex[:12]
        holdout = self.settings.campaign_default_holdout_pct if holdout_pct is None else holdout_pct
        async with self.db.engine.begin() as conn:
            await conn.execute(
                insert(campaigns).values(
                    tenant=tenant,
                    id=cid,
                    name=name,
                    kind=kind,
                    segment=segment,
                    channel=channel,
                    template=text,
                    status="pending_approval" if check.ok else "draft",
                    version=1,
                    holdout_pct=holdout,
                    compliance=check.to_dict(),
                    created_at=utcnow(),
                    created_by=actor,
                )
            )
            if members:
                await conn.execute(
                    insert(recipients),
                    [
                        {
                            "tenant": tenant,
                            "campaign_id": cid,
                            "patient_id": pid,
                            "arm": arm_for(cid, pid, holdout),
                            "status": "pending",
                            "attempts": 0,
                        }
                        for pid in members
                    ],
                )
            await self.audit.record_in(
                conn,
                tenant,
                actor,
                "campaign.created",
                f"campaign/{cid}",
                details={
                    "segment": segment,
                    "recipients": len(members),
                    "compliance": check.to_dict(),
                },
            )
        return await self.get(tenant, cid)

    async def get(self, tenant: str, campaign_id: str) -> dict[str, Any]:
        query = select(campaigns).where(
            and_(campaigns.c.tenant == tenant, campaigns.c.id == campaign_id)
        )
        async with self.db.engine.connect() as conn:
            row = (await conn.execute(query)).mappings().first()
            if row is None:
                raise KeyError(campaign_id)
            counts = (
                await conn.execute(
                    select(recipients.c.arm, recipients.c.status, func.count())
                    .where(
                        and_(recipients.c.tenant == tenant, recipients.c.campaign_id == campaign_id)
                    )
                    .group_by(recipients.c.arm, recipients.c.status)
                )
            ).all()
        out = _campaign(row)
        out["recipients"] = {f"{arm}:{status}": int(n) for arm, status, n in counts}
        return out

    async def list_all(self, tenant: str) -> list[dict[str, Any]]:
        query = (
            select(campaigns)
            .where(campaigns.c.tenant == tenant)
            .order_by(campaigns.c.created_at.desc())
        )
        async with self.db.engine.connect() as conn:
            rows = (await conn.execute(query)).mappings().all()
        return [_campaign(r) for r in rows]

    def _where(self, tenant: str, campaign_id: str, *extra: Any) -> Any:
        return and_(campaigns.c.tenant == tenant, campaigns.c.id == campaign_id, *extra)

    async def update_template(
        self, tenant: str, campaign_id: str, template: str, actor: str
    ) -> dict[str, Any]:
        current = await self.get(tenant, campaign_id)
        if current["status"] not in EDITABLE:
            raise CampaignError(f"campaign is {current['status']}; the copy can no longer change")
        check = self._check(tenant, template)
        # Conditional on what was read: a late edit cannot overwrite a newer state.
        query = (
            update(campaigns)
            .where(
                self._where(
                    tenant,
                    campaign_id,
                    campaigns.c.status.in_(EDITABLE),
                    campaigns.c.version == current["version"],
                )
            )
            .values(
                template=template,
                compliance=check.to_dict(),
                status="pending_approval" if check.ok else "draft",
                version=current["version"] + 1,
            )
        )
        async with self.db.engine.begin() as conn:
            if (await conn.execute(query)).rowcount != 1:
                raise CampaignError("campaign changed while editing; reload it")
            await self.audit.record_in(
                conn,
                tenant,
                actor,
                "campaign.edited",
                f"campaign/{campaign_id}",
                details={**check.to_dict(), "version": current["version"] + 1},
            )
        return await self.get(tenant, campaign_id)

    async def approve(
        self, tenant: str, campaign_id: str, reviewer: str, *, owner_approval: bool = False
    ) -> dict[str, Any]:
        current = await self.get(tenant, campaign_id)
        if current["status"] != "pending_approval":
            raise CampaignError(f"campaign is {current['status']}, not pending approval")
        if current["compliance"].get("needs_owner_approval") and not owner_approval:
            raise CampaignError("the discount exceeds the pack's cap: the owner must approve it")
        query = (
            update(campaigns)
            .where(
                self._where(
                    tenant,
                    campaign_id,
                    campaigns.c.status == "pending_approval",
                    campaigns.c.version == current["version"],
                )
            )
            .values(
                status="approved",
                approved_by=reviewer,
                approved_at=utcnow(),
                approved_version=current["version"],
                approved_hash=text_hash(current["template"]),
            )
        )
        async with self.db.engine.begin() as conn:
            if (await conn.execute(query)).rowcount != 1:
                raise CampaignError("campaign changed while approving; reload it")
            await self.audit.record_in(
                conn,
                tenant,
                reviewer,
                "campaign.approved",
                f"campaign/{campaign_id}",
                details={"owner_approval": owner_approval, "version": current["version"]},
            )
        return await self.get(tenant, campaign_id)

    async def cancel(self, tenant: str, campaign_id: str, actor: str) -> dict[str, Any]:
        """Also while it is being delivered: the worker checks the status before each
        message, and every recipient still queued is marked cancelled here."""
        current = await self.get(tenant, campaign_id)
        if current["status"] not in CANCELLABLE:
            raise CampaignError(f"a {current['status']} campaign cannot be cancelled")
        query = (
            update(campaigns)
            .where(self._where(tenant, campaign_id, campaigns.c.status.in_(CANCELLABLE)))
            .values(status="cancelled")
        )
        async with self.db.engine.begin() as conn:
            if (await conn.execute(query)).rowcount != 1:
                raise CampaignError("campaign changed while cancelling; reload it")
            await conn.execute(
                update(recipients)
                .where(
                    and_(
                        recipients.c.tenant == tenant,
                        recipients.c.campaign_id == campaign_id,
                        recipients.c.status.in_(("pending", "queued")),
                    )
                )
                .values(status="cancelled")
            )
            await self.audit.record_in(
                conn, tenant, actor, "campaign.cancelled", f"campaign/{campaign_id}"
            )
        return await self.get(tenant, campaign_id)

    # --- queueing and delivery (outbox) -------------------------------------------------
    async def send(self, tenant: str, campaign_id: str, actor: str) -> dict[str, Any]:
        """Queue the approved version and deliver it now (the outbox worker finishes or
        resumes anything this call does not, e.g. after a crash)."""
        current = await self.get(tenant, campaign_id)
        if current["status"] != "approved":
            raise CampaignError(
                f"campaign is {current['status']}; only approved campaigns are sent"
            )
        if current["approved_version"] != current["version"] or current[
            "approved_hash"
        ] != text_hash(current["template"]):
            raise CampaignError("the text changed after it was approved; approve it again")
        now = utcnow()
        claim = (
            update(campaigns)
            .where(
                self._where(
                    tenant,
                    campaign_id,
                    campaigns.c.status == "approved",
                    campaigns.c.version == current["version"],
                )
            )
            .values(status="sending", sent_at=now)
        )
        mine = and_(recipients.c.tenant == tenant, recipients.c.campaign_id == campaign_id)
        async with self.db.engine.begin() as conn:
            if (await conn.execute(claim)).rowcount != 1:
                raise CampaignError("campaign is already being sent")
            await conn.execute(
                update(recipients)
                .where(
                    and_(mine, recipients.c.status == "pending", recipients.c.arm == "treatment")
                )
                .values(status="queued")
            )
            # Control members get nothing, and the same reference time for the lift.
            await conn.execute(
                update(recipients)
                .where(and_(mine, recipients.c.status == "pending", recipients.c.arm == "control"))
                .values(status="held_out", sent_at=now)
            )
            await self.audit.record_in(
                conn,
                tenant,
                actor,
                "campaign.queued",
                f"campaign/{campaign_id}",
                details={"version": current["version"], "live": self.telegram.live},
            )
        await self.dispatch(tenant, campaign_id)
        out = await self.get(tenant, campaign_id)
        out["outcomes"] = {k.split(":", 1)[1]: v for k, v in out["recipients"].items()}
        return out

    async def _has_app(self, tenant: str, patient_id: str, now: datetime) -> bool:
        query = select(func.count()).where(
            and_(
                principals.c.tenant == tenant,
                principals.c.kind == PATIENT,
                principals.c.subject_id == patient_id,
                principals.c.revoked_at.is_(None),
                or_(principals.c.expires_at.is_(None), principals.c.expires_at > now),
            )
        )
        async with self.db.engine.connect() as conn:
            return int((await conn.execute(query)).scalar_one()) > 0

    async def _reserve(self, tenant: str, patient_id: str, cap: int, now: datetime) -> bool:
        """Take one slot of the patient's monthly cap, or return False if it is full."""
        key = and_(
            contact_budget.c.tenant == tenant,
            contact_budget.c.patient_id == patient_id,
            contact_budget.c.period == _period(now),
        )
        take = (
            update(contact_budget)
            .where(and_(key, contact_budget.c.used < cap))
            .values(used=contact_budget.c.used + 1)
        )
        for _ in range(2):  # a second pass if another worker created the row first
            async with self.db.engine.begin() as conn:
                if (await conn.execute(take)).rowcount == 1:
                    return True
                exists = (await conn.execute(select(contact_budget.c.used).where(key))).first()
                if exists is not None:
                    return False
            if cap < 1:
                return False
            try:
                async with self.db.engine.begin() as conn:
                    await conn.execute(
                        insert(contact_budget).values(
                            tenant=tenant, patient_id=patient_id, period=_period(now), used=1
                        )
                    )
                return True
            except IntegrityError:
                continue
        return False

    async def _release(self, tenant: str, patient_id: str, when: datetime) -> None:
        """Give the slot back: the message was certainly not delivered."""
        async with self.db.engine.begin() as conn:
            await conn.execute(
                update(contact_budget)
                .where(
                    and_(
                        contact_budget.c.tenant == tenant,
                        contact_budget.c.patient_id == patient_id,
                        contact_budget.c.period == _period(when),
                        contact_budget.c.used > 0,
                    )
                )
                .values(used=contact_budget.c.used - 1)
            )

    async def _claim(
        self, tenant: str, campaign_id: str, patient_id: str, from_status: str
    ) -> bool:
        query = (
            update(recipients)
            .where(
                and_(
                    recipients.c.tenant == tenant,
                    recipients.c.campaign_id == campaign_id,
                    recipients.c.patient_id == patient_id,
                    recipients.c.status == from_status,
                )
            )
            .values(status="sending", claimed_at=utcnow(), attempts=recipients.c.attempts + 1)
        )
        async with self.db.engine.begin() as conn:
            return (await conn.execute(query)).rowcount == 1

    async def _unclaim(
        self, tenant: str, campaign_id: str, patient_id: str, to_status: str
    ) -> None:
        await self._record(tenant, campaign_id, patient_id, status=to_status, claimed_at=None)

    async def _record(self, tenant: str, campaign_id: str, patient_id: str, **values: Any) -> None:
        async with self.db.engine.begin() as conn:
            await conn.execute(
                update(recipients)
                .where(
                    and_(
                        recipients.c.tenant == tenant,
                        recipients.c.campaign_id == campaign_id,
                        recipients.c.patient_id == patient_id,
                        recipients.c.status == "sending",
                    )
                )
                .values(**values)
            )

    async def _deliver(
        self, tenant: str, campaign: dict[str, Any], patient_id: str, fallback: bool
    ) -> str:
        """One recipient, already claimed (status `sending`). Everything that can change
        between queueing and now is checked right before the message goes out. An error
        in those checks raises _NotSent (the claim can be undone); an error after the
        message may have left keeps the claim, so it ends up `uncertain`, not re-sent."""
        cid = campaign["id"]
        now = utcnow()
        try:
            decided = await self._decide(tenant, campaign, patient_id, fallback, now)
        except Exception as exc:
            raise _NotSent() from exc
        if isinstance(decided, str):
            return decided
        chat_id, text = decided
        try:
            status = await self.telegram.send(chat_id, text)
        except DeliveryUncertain as exc:
            log.warning("campaign %s: delivery uncertain (%s)", cid, exc)
            # The slot stays taken and the message is not retried: it may have arrived.
            await self._record(tenant, cid, patient_id, status="uncertain", error=str(exc)[:200])
            return "uncertain"
        except Exception as exc:
            log.warning("campaign %s: delivery failed (%s)", cid, type(exc).__name__)
            await self._release(tenant, patient_id, now)
            await self._record(tenant, cid, patient_id, status="failed", error=type(exc).__name__)
            return "failed"
        await self._record(
            tenant, cid, patient_id, status=status, channel="telegram", sent_at=utcnow()
        )
        return status

    async def _decide(
        self,
        tenant: str,
        campaign: dict[str, Any],
        patient_id: str,
        fallback: bool,
        now: datetime,
    ) -> str | tuple[str, str]:
        """The final status when nothing is sent by message (skips, in-app delivery), or
        (chat_id, text) with a cap slot already reserved."""
        cid = campaign["id"]
        if not await self.consents.has(tenant, patient_id, Purpose.MARKETING):
            await self._record(tenant, cid, patient_id, status="skipped_no_consent")
            return "skipped_no_consent"
        info = (await self.crm.contacts(tenant, [patient_id])).get(patient_id) or {}
        if not info or info.get("restricted"):
            await self._record(tenant, cid, patient_id, status="skipped_no_channel")
            return "skipped_no_channel"
        chat_id = info.get("telegram_chat_id")
        if not fallback and await self._has_app(tenant, patient_id, now):
            hours = self.settings.campaign_fallback_hours
            due = now + timedelta(hours=hours) if chat_id and hours > 0 else None
            await self._record(
                tenant,
                cid,
                patient_id,
                status="in_app",
                channel="app",
                sent_at=now,
                fallback_due_at=due,
            )
            return "in_app"
        if not chat_id:
            await self._record(tenant, cid, patient_id, status="skipped_no_channel")
            return "skipped_no_channel"
        cap = pack_for(self.settings, tenant).campaigns.max_messages_per_month
        if not await self._reserve(tenant, patient_id, cap, now):
            await self._record(tenant, cid, patient_id, status="skipped_cap")
            return "skipped_cap"
        return str(chat_id), render(campaign["template"], info.get("display_name") or "")

    async def dispatch(self, tenant: str, campaign_id: str) -> dict[str, int]:
        """Deliver every queued recipient of one campaign, one claim at a time."""
        query = select(recipients.c.patient_id).where(
            and_(
                recipients.c.tenant == tenant,
                recipients.c.campaign_id == campaign_id,
                recipients.c.status == "queued",
            )
        )
        async with self.db.engine.connect() as conn:
            queued: list[str] = list((await conn.execute(query)).scalars().all())
        tally: dict[str, int] = {}
        for pid in queued:
            campaign = await self.get(tenant, campaign_id)
            if campaign["status"] != "sending":  # cancelled meanwhile: stop here
                break
            if not await self._claim(tenant, campaign_id, pid, "queued"):
                continue  # another worker has it
            try:
                outcome = await self._deliver(tenant, campaign, pid, fallback=False)
            except _NotSent as exc:
                # Nothing left: back to the queue, for this call's caller or the worker.
                await self._unclaim(tenant, campaign_id, pid, "queued")
                raise exc.__cause__ or exc from None
            tally[outcome] = tally.get(outcome, 0) + 1
        await self._finish(tenant, campaign_id)
        return tally

    async def _finish(self, tenant: str, campaign_id: str) -> None:
        """sending -> completed | partial_failed once no recipient is left in flight."""
        counts = (await self.get(tenant, campaign_id))["recipients"]
        by_status: dict[str, int] = {}
        for key, n in counts.items():
            status = key.split(":", 1)[1]
            by_status[status] = by_status.get(status, 0) + n
        if by_status.get("queued") or by_status.get("sending"):
            return
        final = "partial_failed" if any(by_status.get(s) for s in _FINAL_FAILURES) else "completed"
        async with self.db.engine.begin() as conn:
            done = await conn.execute(
                update(campaigns)
                .where(self._where(tenant, campaign_id, campaigns.c.status == "sending"))
                .values(status=final, completed_at=utcnow())
            )
            if done.rowcount == 1:
                await self.audit.record_in(
                    conn,
                    tenant,
                    "worker:outbox",
                    f"campaign.{final}",
                    f"campaign/{campaign_id}",
                    details={"outcomes": by_status},
                )

    async def run_outbox(self) -> dict[str, int]:
        """The worker's pass (any replica, any tenant): mark stale claims uncertain,
        deliver queued recipients, send the Telegram fallback of unseen in-app offers."""
        now = utcnow()
        stale = now - timedelta(seconds=self.settings.outbox_stale_seconds)
        async with self.db.engine.begin() as conn:
            # A claim this old belongs to a worker that died mid-delivery: the message
            # may or may not have gone out, so it is not sent again automatically.
            recovered = (
                await conn.execute(
                    update(recipients)
                    .where(and_(recipients.c.status == "sending", recipients.c.claimed_at < stale))
                    .values(status="uncertain", error="worker stopped mid-delivery")
                )
            ).rowcount
            active = (
                await conn.execute(
                    select(campaigns.c.tenant, campaigns.c.id).where(
                        campaigns.c.status == "sending"
                    )
                )
            ).all()
            due = (
                await conn.execute(
                    select(recipients.c.tenant, recipients.c.campaign_id, recipients.c.patient_id)
                    .select_from(
                        recipients.join(
                            campaigns,
                            and_(
                                campaigns.c.tenant == recipients.c.tenant,
                                campaigns.c.id == recipients.c.campaign_id,
                            ),
                        )
                    )
                    .where(
                        and_(
                            recipients.c.status == "in_app",
                            recipients.c.seen_at.is_(None),
                            recipients.c.fallback_due_at <= now,
                            campaigns.c.status.in_(SENT_STATES),
                        )
                    )
                )
            ).all()
        stats = {"recovered": int(recovered), "campaigns": 0, "fallback": 0}
        for tenant, cid in active:
            await self.dispatch(tenant, cid)
            stats["campaigns"] += 1
        for tenant, cid, pid in due:
            if not await self._claim(tenant, cid, pid, "in_app"):
                continue
            try:
                await self._deliver(tenant, await self.get(tenant, cid), pid, fallback=True)
            except _NotSent:
                await self._unclaim(tenant, cid, pid, "in_app")  # next pass tries again
                log.exception("campaign %s: fallback not attempted", cid)
                continue
            stats["fallback"] += 1
        return stats

    async def retry_failed(self, tenant: str, campaign_id: str, actor: str) -> dict[str, Any]:
        """Queue again the recipients whose delivery certainly failed (never `uncertain`)."""
        async with self.db.engine.begin() as conn:
            moved = await conn.execute(
                update(recipients)
                .where(
                    and_(
                        recipients.c.tenant == tenant,
                        recipients.c.campaign_id == campaign_id,
                        recipients.c.status == "failed",
                    )
                )
                .values(status="queued", error=None)
            )
            if moved.rowcount == 0:
                raise CampaignError("no failed deliveries to retry")
            await conn.execute(
                update(campaigns)
                .where(
                    self._where(
                        tenant, campaign_id, campaigns.c.status.in_(("partial_failed", "sending"))
                    )
                )
                .values(status="sending", completed_at=None)
            )
            await self.audit.record_in(
                conn,
                tenant,
                actor,
                "campaign.retry",
                f"campaign/{campaign_id}",
                details={"recipients": moved.rowcount},
            )
        await self.dispatch(tenant, campaign_id)
        return await self.get(tenant, campaign_id)

    async def resolve_uncertain(
        self, tenant: str, campaign_id: str, patient_id: str, delivered: bool, actor: str
    ) -> dict[str, Any]:
        """An operator checked (e.g. in the bot's chat history) whether it arrived."""
        async with self.db.engine.begin() as conn:
            done = await conn.execute(
                update(recipients)
                .where(
                    and_(
                        recipients.c.tenant == tenant,
                        recipients.c.campaign_id == campaign_id,
                        recipients.c.patient_id == patient_id,
                        recipients.c.status == "uncertain",
                    )
                )
                .values(
                    status="sent" if delivered else "failed",
                    channel="telegram",
                    sent_at=utcnow() if delivered else None,
                )
            )
            if done.rowcount != 1:
                raise CampaignError("no uncertain delivery for this recipient")
            await self.audit.record_in(
                conn,
                tenant,
                actor,
                "campaign.delivery_resolved",
                f"campaign/{campaign_id}",
                subject_id=patient_id,
                details={"delivered": delivered},
            )
        if not delivered:
            await self._release(tenant, patient_id, utcnow())
        return await self.get(tenant, campaign_id)

    # --- inbound: opt-out by reply ------------------------------------------------------
    async def handle_inbound(self, tenant: str, chat_id: str, text: str) -> str:
        """A message to the bot. STOP (or BAJA...) withdraws the marketing consent of
        every patient of this tenant behind that chat, at once and idempotently."""
        if not is_stop(text):
            return "ignored"
        query = select(patients.c.id).where(
            and_(patients.c.tenant == tenant, patients.c.telegram_chat_id == chat_id)
        )
        async with self.db.engine.connect() as conn:
            ids: list[str] = list((await conn.execute(query)).scalars().all())
        if not ids:
            return "unknown_sender"
        for pid in ids:
            if await self.consents.has(tenant, pid, Purpose.MARKETING):
                await self.consents.record(
                    tenant,
                    pid,
                    Purpose.MARKETING,
                    False,
                    source="telegram:STOP",
                    actor="channel:telegram",
                )
        try:
            await self.telegram.send(chat_id, STOP_CONFIRMATION)
        except Exception as exc:  # the opt-out stands even if the confirmation fails
            log.warning("STOP confirmation not delivered (%s)", type(exc).__name__)
        return "unsubscribed"

    async def mark_seen(self, tenant: str, patient_id: str) -> int:
        """The patient opened their offers in the app: no Telegram fallback for them."""
        async with self.db.engine.begin() as conn:
            return int(
                (
                    await conn.execute(
                        update(recipients)
                        .where(
                            and_(
                                recipients.c.tenant == tenant,
                                recipients.c.patient_id == patient_id,
                                recipients.c.status == "in_app",
                            )
                        )
                        .values(status="seen", seen_at=utcnow())
                    )
                ).rowcount
            )

    async def results(self, tenant: str, campaign_id: str) -> dict[str, Any]:
        """Booking rate per arm within the conversion window, lift and a z-test.
        The treatment arm is the recipients reached (in the app or by message), the
        control arm those held out."""
        campaign = await self.get(tenant, campaign_id)
        if campaign["status"] not in SENT_STATES:
            raise CampaignError("results exist only for sent campaigns")
        window = timedelta(days=self.settings.campaign_conversion_window_days)
        query = select(recipients).where(
            and_(
                recipients.c.tenant == tenant,
                recipients.c.campaign_id == campaign_id,
                recipients.c.status.in_((*REACHED, "held_out")),
                # Anonymised rows lost the link to their bookings: they cannot be scored.
                ~recipients.c.patient_id.startswith(ANON_PREFIX),
            )
        )
        arms: dict[str, dict[str, int]] = {
            "treatment": {"n": 0, "converted": 0},
            "control": {"n": 0, "converted": 0},
        }
        async with self.db.engine.connect() as conn:
            rows = (await conn.execute(query)).mappings().all()
            if not rows:
                bookings: dict[str, list[datetime]] = {}
            else:
                starts = [aware(r["sent_at"]) or utcnow() for r in rows]
                booked_rows = (
                    await conn.execute(
                        select(appointments.c.patient_id, appointments.c.created_at).where(
                            and_(
                                appointments.c.tenant == tenant,
                                appointments.c.patient_id.in_([r["patient_id"] for r in rows]),
                                appointments.c.created_at > min(starts),
                                appointments.c.created_at <= max(starts) + window,
                            )
                        )
                    )
                ).all()
                bookings = {}
                for pid, created in booked_rows:
                    bookings.setdefault(pid, []).append(aware(created) or created)
        for row in rows:
            sent_at = aware(row["sent_at"]) or utcnow()
            converted = any(
                sent_at < created <= sent_at + window
                for created in bookings.get(row["patient_id"], [])
            )
            arm = arms[row["arm"]]
            arm["n"] += 1
            arm["converted"] += 1 if converted else 0
        rate = {k: (v["converted"] / v["n"] if v["n"] else 0.0) for k, v in arms.items()}
        p_value = two_proportion_p(
            arms["treatment"]["converted"],
            arms["treatment"]["n"],
            arms["control"]["converted"],
            arms["control"]["n"],
        )
        small = min(arms["treatment"]["n"], arms["control"]["n"]) < MIN_ARM_SIZE
        return {
            "campaign_id": campaign_id,
            "window_days": self.settings.campaign_conversion_window_days,
            "arms": {k: {**v, "rate": round(rate[k], 4)} for k, v in arms.items()},
            "lift_abs": round(rate["treatment"] - rate["control"], 4),
            "lift_rel": round(rate["treatment"] / rate["control"] - 1, 4)
            if rate["control"]
            else None,
            "p_value": round(p_value, 4) if p_value is not None else None,
            "conclusion": "inconclusive (arms too small)"
            if small
            else (
                "significant at 5%" if p_value is not None and p_value < 0.05 else "not significant"
            ),
        }

    async def offers_for(self, tenant: str, patient_id: str) -> list[dict[str, Any]]:
        """Offers this patient actually received: in the app or by message. Dry runs
        and held-out members received nothing, so they are not listed."""
        query = (
            select(
                campaigns.c.kind,
                campaigns.c.name,
                campaigns.c.template,
                recipients.c.sent_at,
                recipients.c.seen_at,
                recipients.c.status,
                patients.c.display_name,
            )
            .select_from(
                recipients.join(
                    campaigns,
                    and_(
                        campaigns.c.tenant == recipients.c.tenant,
                        campaigns.c.id == recipients.c.campaign_id,
                    ),
                ).join(
                    patients,
                    and_(
                        patients.c.tenant == recipients.c.tenant,
                        patients.c.id == recipients.c.patient_id,
                    ),
                )
            )
            .where(
                and_(
                    recipients.c.tenant == tenant,
                    recipients.c.patient_id == patient_id,
                    recipients.c.status.in_(("in_app", "seen", "sent")),
                )
            )
            .order_by(recipients.c.sent_at.desc())
        )
        async with self.db.engine.connect() as conn:
            rows = (await conn.execute(query)).mappings().all()
        return [
            {
                "kind": r["kind"],
                "campaign": r["name"],
                "text": render(r["template"], r["display_name"]),
                "sent_at": (aware(r["sent_at"]) or utcnow()).isoformat(),
                "seen": r["status"] != "in_app",
            }
            for r in rows
        ]

    # --- data-subject rights -------------------------------------------------
    async def export_subject(self, tenant: str, patient_id: str) -> list[dict[str, Any]]:
        query = select(recipients).where(
            and_(recipients.c.tenant == tenant, recipients.c.patient_id == patient_id)
        )
        async with self.db.engine.connect() as conn:
            rows = (await conn.execute(query)).mappings().all()
        out = []
        for r in rows:
            item = dict(r)
            for key in ("sent_at", "seen_at", "claimed_at", "fallback_due_at"):
                value = aware(item.get(key))
                item[key] = value.isoformat() if value else None
            out.append(item)
        return out

    async def erase_subject(self, tenant: str, patient_id: str) -> int:
        """The marketing history stops being about this person. Each delivery row keeps
        its campaign, arm and outcome (the business keeps learning what works) but loses
        the link to the patient and every timestamp: a random id replaces the patient id,
        so it is anonymous, not pseudonymous (a hash of the id could be recomputed)."""
        query = select(recipients.c.campaign_id).where(
            and_(recipients.c.tenant == tenant, recipients.c.patient_id == patient_id)
        )
        async with self.db.engine.begin() as conn:
            rows: list[str] = list((await conn.execute(query)).scalars().all())
            for campaign_id in rows:
                await conn.execute(
                    update(recipients)
                    .where(
                        and_(
                            recipients.c.tenant == tenant,
                            recipients.c.campaign_id == campaign_id,
                            recipients.c.patient_id == patient_id,
                        )
                    )
                    .values(
                        patient_id=f"{ANON_PREFIX}{uuid.uuid4().hex}",
                        sent_at=None,
                        seen_at=None,
                        claimed_at=None,
                        fallback_due_at=None,
                        error=None,
                    )
                )
            await conn.execute(
                delete(contact_budget).where(
                    and_(
                        contact_budget.c.tenant == tenant,
                        contact_budget.c.patient_id == patient_id,
                    )
                )
            )
        return len(rows)
