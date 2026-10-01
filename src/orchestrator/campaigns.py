"""Loyalty campaigns with a holdout group, compliance checks and human approval.

    insight segment -> draft (copy checked against the pack) -> approve (a person)
    -> send to the treatment arm (the control arm gets nothing) -> measure lift

- Recipients are fixed when the campaign is created: members of the segment, minus
  restricted records. Each one is then eligible only with the `marketing` consent, an
  address on the channel and room under the monthly frequency cap; consent is checked
  again at send time (it can be withdrawn in between).
- The control arm is chosen by a hash of (campaign, subject), so the split is
  reproducible and does not depend on the order recipients were listed in.
- Lift is the difference in booking rate between arms within the conversion window,
  with a two-proportion z-test. With small arms the result is labelled as inconclusive
  instead of being reported as an effect.
- Messages carry no clinical details (checked) and are personalised only with the first
  name. Without TELEGRAM_BOT_TOKEN the channel runs dry: messages are recorded, not sent."""

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
    select,
    update,
)

from orchestrator.config import Settings
from orchestrator.crm import CrmService, appointments, patients
from orchestrator.db import Database, aware, metadata, utcnow
from orchestrator.governance import AuditLog, ConsentRegistry, Purpose
from orchestrator.insights import InsightsService
from orchestrator.llm import LLMClient
from orchestrator.packs import pack_for
from orchestrator.risk import CopyCheck, check_copy

log = logging.getLogger(__name__)

KINDS = ("recall", "reactivation", "pending_treatment", "referral", "birthday", "education")
CHANNELS = ("telegram",)
_PLACEHOLDER = re.compile(r"\{(\w+)\}")
ALLOWED_PLACEHOLDERS = {"first_name"}
MIN_ARM_SIZE = 30

COPYWRITER_PROMPT = """You are the COPYWRITER of a small business. Write one short, warm
message (max 320 characters) for a {kind} campaign sent to customers in the "{segment}"
segment. Use {{first_name}} for the customer's first name and no other placeholder. Never
mention health conditions, treatments, diagnoses or prices, never promise results, and
always let the customer opt out by replying STOP. Language: {language}. Reply with the
message only."""

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
    Column("status", String(24), nullable=False),  # draft|pending_approval|approved|sent|cancelled
    Column("holdout_pct", Integer, nullable=False),
    Column("compliance", JSON, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("created_by", String(128), nullable=False),
    Column("approved_by", String(128), nullable=True),
    Column("approved_at", DateTime(timezone=True), nullable=True),
    Column("sent_at", DateTime(timezone=True), nullable=True),
)

recipients = Table(
    "campaign_recipients",
    metadata,
    Column("tenant", String(64), primary_key=True),
    Column("campaign_id", String(64), primary_key=True),
    Column("patient_id", String(64), primary_key=True),
    Column("arm", String(16), nullable=False),  # treatment|control
    # pending|sent|dry_run|failed|skipped_no_consent|skipped_no_channel|skipped_cap|held_out
    Column("status", String(24), nullable=False),
    Column("sent_at", DateTime(timezone=True), nullable=True),
    Column("error", String(200), nullable=True),
)

_SENT = ("sent", "dry_run")


class CampaignError(ValueError):
    pass


def arm_for(campaign_id: str, patient_id: str, holdout_pct: int) -> str:
    digest = hashlib.sha256(f"{campaign_id}:{patient_id}".encode()).digest()
    return "control" if int.from_bytes(digest[:4], "big") % 100 < holdout_pct else "treatment"


def placeholders_ok(template: str) -> list[str]:
    return sorted({p for p in _PLACEHOLDER.findall(template) if p not in ALLOWED_PLACEHOLDERS})


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
        """'sent' or 'dry_run'; raises on delivery errors."""
        if self.settings.telegram_bot_token is None:
            return "dry_run"
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=10.0)
        token = self.settings.telegram_bot_token.get_secret_value()
        resp = await self._http.post(
            f"{self.settings.telegram_api_base}/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text},
        )
        if resp.status_code != 200 or not resp.json().get("ok"):
            # Never include the URL: it contains the bot token.
            raise RuntimeError(f"telegram HTTP {resp.status_code}")
        return "sent"

    async def close(self) -> None:
        if self._http is not None:
            await self._http.aclose()


def _campaign(row: Any) -> dict[str, Any]:
    out = dict(row)
    for key in ("created_at", "approved_at", "sent_at"):
        value = aware(out.get(key))
        out[key] = value.isoformat() if value else None
    return out


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
        if "stop" not in template.lower():
            check.ok = False
            check.violations.append("missing_opt_out:reply STOP")
        return check

    async def draft_copy(self, kind: str, segment: str, language: str) -> str:
        result = await self.llm.complete(
            [
                {
                    "role": "system",
                    "content": COPYWRITER_PROMPT.format(
                        kind=kind, segment=segment, language=language
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
        return (
            text
            if "stop" in text.lower()
            else f"{text} Responde STOP para no recibir más mensajes."
        )

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
                        }
                        for pid in members
                    ],
                )
        await self.audit.record(
            tenant,
            actor,
            "campaign.created",
            f"campaign/{cid}",
            details={"segment": segment, "recipients": len(members), "compliance": check.to_dict()},
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

    async def update_template(
        self, tenant: str, campaign_id: str, template: str, actor: str
    ) -> dict[str, Any]:
        current = await self.get(tenant, campaign_id)
        if current["status"] not in ("draft", "pending_approval"):
            raise CampaignError(f"campaign is {current['status']}; the copy can no longer change")
        check = self._check(tenant, template)
        query = (
            update(campaigns)
            .where(and_(campaigns.c.tenant == tenant, campaigns.c.id == campaign_id))
            .values(
                template=template,
                compliance=check.to_dict(),
                status="pending_approval" if check.ok else "draft",
            )
        )
        async with self.db.engine.begin() as conn:
            await conn.execute(query)
        await self.audit.record(
            tenant, actor, "campaign.edited", f"campaign/{campaign_id}", details=check.to_dict()
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
                and_(
                    campaigns.c.tenant == tenant,
                    campaigns.c.id == campaign_id,
                    campaigns.c.status == "pending_approval",
                )
            )
            .values(status="approved", approved_by=reviewer, approved_at=utcnow())
        )
        async with self.db.engine.begin() as conn:
            if (await conn.execute(query)).rowcount != 1:
                raise CampaignError("campaign changed while approving; reload it")
        await self.audit.record(
            tenant,
            reviewer,
            "campaign.approved",
            f"campaign/{campaign_id}",
            details={"owner_approval": owner_approval},
        )
        return await self.get(tenant, campaign_id)

    async def cancel(self, tenant: str, campaign_id: str, actor: str) -> dict[str, Any]:
        current = await self.get(tenant, campaign_id)
        if current["status"] == "sent":
            raise CampaignError("a sent campaign cannot be cancelled")
        query = (
            update(campaigns)
            .where(and_(campaigns.c.tenant == tenant, campaigns.c.id == campaign_id))
            .values(status="cancelled")
        )
        async with self.db.engine.begin() as conn:
            await conn.execute(query)
        await self.audit.record(tenant, actor, "campaign.cancelled", f"campaign/{campaign_id}")
        return await self.get(tenant, campaign_id)

    async def _sent_last_month(
        self, tenant: str, patient_ids: list[str], now: datetime
    ) -> dict[str, int]:
        """Messages delivered to each patient in the last 30 days, across campaigns."""
        if not patient_ids:
            return {}
        query = (
            select(recipients.c.patient_id, func.count())
            .where(
                and_(
                    recipients.c.tenant == tenant,
                    recipients.c.patient_id.in_(patient_ids),
                    recipients.c.status.in_(_SENT),
                    recipients.c.sent_at >= now - timedelta(days=30),
                )
            )
            .group_by(recipients.c.patient_id)
        )
        async with self.db.engine.connect() as conn:
            return {pid: int(n) for pid, n in (await conn.execute(query)).all()}

    async def send(self, tenant: str, campaign_id: str, actor: str) -> dict[str, Any]:
        current = await self.get(tenant, campaign_id)
        if current["status"] != "approved":
            raise CampaignError(
                f"campaign is {current['status']}; only approved campaigns are sent"
            )
        # Claim it first so two concurrent sends cannot both deliver.
        claim = (
            update(campaigns)
            .where(
                and_(
                    campaigns.c.tenant == tenant,
                    campaigns.c.id == campaign_id,
                    campaigns.c.status == "approved",
                )
            )
            .values(status="sent", sent_at=utcnow())
        )
        async with self.db.engine.begin() as conn:
            if (await conn.execute(claim)).rowcount != 1:
                raise CampaignError("campaign is already being sent")
        cap = pack_for(self.settings, tenant).campaigns.max_messages_per_month
        consented = await self.consents.granted_subjects(tenant, Purpose.MARKETING)
        query = select(recipients).where(
            and_(recipients.c.tenant == tenant, recipients.c.campaign_id == campaign_id)
        )
        async with self.db.engine.connect() as conn:
            rows = (await conn.execute(query)).mappings().all()
        ids = [r["patient_id"] for r in rows]
        contacts = await self.crm.contacts(tenant, ids)
        # This campaign's own recipients are still "pending", so they are not counted.
        sent_recently = await self._sent_last_month(tenant, ids, utcnow())
        tally: dict[str, int] = {}
        for row in rows:
            pid = row["patient_id"]
            now = utcnow()
            error = None
            info = contacts.get(pid) or {}
            if row["arm"] == "control":
                status = "held_out"
            elif pid not in consented:
                status = "skipped_no_consent"
            elif info.get("restricted") or not info.get("telegram_chat_id"):
                status = "skipped_no_channel"
            elif sent_recently.get(pid, 0) >= cap:
                status = "skipped_cap"
            else:
                try:
                    status = await self.telegram.send(
                        info["telegram_chat_id"],
                        render(current["template"], info.get("display_name") or ""),
                    )
                except Exception as exc:
                    log.warning(
                        "campaign %s: delivery failed (%s)", campaign_id, type(exc).__name__
                    )
                    status, error = "failed", type(exc).__name__
            values: dict[str, Any] = {"status": status, "error": error}
            if status in _SENT or status == "held_out":
                values["sent_at"] = now  # control members get the same reference time
            async with self.db.engine.begin() as conn:
                await conn.execute(
                    update(recipients)
                    .where(
                        and_(
                            recipients.c.tenant == tenant,
                            recipients.c.campaign_id == campaign_id,
                            recipients.c.patient_id == pid,
                        )
                    )
                    .values(**values)
                )
            tally[status] = tally.get(status, 0) + 1
        await self.audit.record(
            tenant,
            actor,
            "campaign.sent",
            f"campaign/{campaign_id}",
            details={"outcomes": tally, "live": self.telegram.live},
        )
        return {**(await self.get(tenant, campaign_id)), "outcomes": tally}

    async def results(self, tenant: str, campaign_id: str) -> dict[str, Any]:
        """Booking rate per arm within the conversion window, lift and a z-test.
        Only recipients who could have been reached count: the treatment arm is those
        messaged, the control arm those held out (the same eligibility is not applied
        to control, which slightly favours it: the lift is conservative)."""
        campaign = await self.get(tenant, campaign_id)
        if campaign["status"] != "sent":
            raise CampaignError("results exist only for sent campaigns")
        window = timedelta(days=self.settings.campaign_conversion_window_days)
        query = select(recipients).where(
            and_(
                recipients.c.tenant == tenant,
                recipients.c.campaign_id == campaign_id,
                recipients.c.status.in_((*_SENT, "held_out")),
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
        """Messages actually delivered to this patient (the patient's own view). Dry runs
        and held-out members received nothing, so they are not listed."""
        query = (
            select(
                campaigns.c.kind,
                campaigns.c.name,
                campaigns.c.template,
                recipients.c.sent_at,
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
                    recipients.c.status == "sent",
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
            sent = aware(item.get("sent_at"))
            item["sent_at"] = sent.isoformat() if sent else None
            out.append(item)
        return out

    async def erase_subject(self, tenant: str, patient_id: str) -> int:
        query = delete(recipients).where(
            and_(recipients.c.tenant == tenant, recipients.c.patient_id == patient_id)
        )
        async with self.db.engine.begin() as conn:
            return int((await conn.execute(query)).rowcount)
