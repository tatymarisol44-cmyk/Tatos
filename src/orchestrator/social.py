"""Social channels (ADR 0015): the accounts each professional connects, and the platform
rules every publication or message must pass before any adapter talks to a platform.

Three things are kept apart on purpose:

* **Rules** (`PLATFORM_RULES`) cite the official page they come from and carry a status,
  like the legal references of the packs: `read` means the page was read, `to_verify`
  means nobody has, and a network whose publishing rules are `to_verify` cannot publish.
* **Accounts** (`channel_accounts`) say which page, number or profile belongs to which
  tenant and, optionally, to which professional of that establishment.
* **Credentials never touch the database.** An account stores `secret_ref`, the *name* of
  a secret; the token itself is read from the environment (`SOCIAL_SECRET_<REF>`), which
  in production is filled from the secret store. The API only says whether it is set."""

from __future__ import annotations

import os
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

from sqlalchemy import Boolean, Column, DateTime, Index, String, Table, and_, insert, select, update

from orchestrator.db import Database, metadata, utcnow
from orchestrator.establishment import Professionals
from orchestrator.governance import AuditLog

Network = Literal["whatsapp", "instagram", "facebook", "tiktok", "telegram"]
NETWORKS: tuple[Network, ...] = ("whatsapp", "instagram", "facebook", "tiktok", "telegram")
# Networks that publish posts; the others only exchange messages.
PUBLISHING: tuple[Network, ...] = ("instagram", "facebook", "tiktok")
MediaType = Literal["image", "video", "carousel", "text"]

# An account names its credential (`secret_ref`); this is the shape of that name, and the
# environment variable that holds the value is CREDENTIAL_ENV_PREFIX + name.
REF_PATTERN = r"^[A-Z][A-Z0-9_]{2,63}$"
CREDENTIAL_ENV_PREFIX = "SOCIAL_SECRET_"


@dataclass(frozen=True)
class PlatformRule:
    network: Network
    id: str
    rule: str
    source: str
    status: Literal["read", "to_verify"]


_WA_TEMPLATES = "https://developers.facebook.com/docs/whatsapp/message-templates/guidelines"
_WA_POLICY = "https://whatsappbusiness.com/es-la/policy/"
_IG_PUBLISHING = "https://developers.facebook.com/docs/instagram-platform/content-publishing"
_TT_POSTING = "https://developers.tiktok.com/doc/content-posting-api-get-started"

# Read on 2026-10-07. Re-read before a channel goes live: platforms change their terms.
PLATFORM_RULES: tuple[PlatformRule, ...] = (
    PlatformRule(
        "whatsapp",
        "WA-WINDOW",
        "Outside a customer service window only template messages may be sent.",
        _WA_TEMPLATES,
        "read",
    ),
    PlatformRule(
        "whatsapp",
        "WA-TEMPLATE-REVIEW",
        "Templates are reviewed by Meta; only APPROVED templates can be sent. Categories: "
        "authentication, marketing, utility.",
        _WA_TEMPLATES,
        "read",
    ),
    PlatformRule(
        "whatsapp",
        "WA-OPT-IN",
        "Contact only people who gave their number and opted in; honour every request to "
        "stop, inside or outside WhatsApp.",
        _WA_POLICY,
        "read",
    ),
    PlatformRule(
        "whatsapp",
        "WA-HEALTH",
        "Do not offer telemedicine or send or request health information where regulations "
        "require systems with stricter handling.",
        _WA_POLICY,
        "read",
    ),
    PlatformRule(
        "whatsapp",
        "WA-HUMAN",
        "During the service window there must be a prompt, clear path to a human.",
        _WA_POLICY,
        "read",
    ),
    PlatformRule(
        "instagram",
        "IG-ACCOUNT",
        "Only Instagram professional accounts can publish through the API.",
        _IG_PUBLISHING,
        "read",
    ),
    PlatformRule(
        "instagram",
        "IG-JPEG",
        "JPEG is the only image format; MPO and JPS are not supported.",
        _IG_PUBLISHING,
        "read",
    ),
    PlatformRule(
        "instagram",
        "IG-PUBLIC-URL",
        "Media must be on a publicly accessible server when the post is published.",
        _IG_PUBLISHING,
        "read",
    ),
    PlatformRule(
        "instagram",
        "IG-LIMIT",
        "At most 100 API-published posts per account in a moving 24 hours; a carousel "
        "counts as one.",
        _IG_PUBLISHING,
        "read",
    ),
    PlatformRule(
        "tiktok",
        "TT-UNAUDITED",
        "Content posted by an unaudited client is restricted to private viewing; an audit "
        "lifts it.",
        _TT_POSTING,
        "read",
    ),
    PlatformRule(
        "tiktok",
        "TT-MEDIA",
        "Videos are MP4 with H.264; photos only from verified-domain URLs. Needs the "
        "video.publish scope.",
        _TT_POSTING,
        "read",
    ),
    PlatformRule(
        "facebook",
        "FB-PAGES",
        "Page publishing rules have not been read yet.",
        "",
        "to_verify",
    ),
)
IG_DAILY_POST_LIMIT = 100


def rules_for(network: Network) -> list[PlatformRule]:
    return [r for r in PLATFORM_RULES if r.network == network]


def network_verified(network: Network) -> bool:
    rules = rules_for(network)
    return bool(rules) and all(r.status == "read" for r in rules)


# --- pre-flight checks (pure: every adapter calls them before touching a platform) ------


@dataclass
class PublishCheck:
    problems: list[str] = field(default_factory=list)
    visibility: Literal["public", "private"] = "public"

    @property
    def allowed(self) -> bool:
        return not self.problems


def check_publish(
    network: Network,
    *,
    media_type: MediaType,
    media_format: str = "",
    public_url: bool = False,
    audited: bool = False,
    posts_last_24h: int = 0,
) -> PublishCheck:
    check = PublishCheck()
    fmt = media_format.lower().lstrip(".")
    if network not in PUBLISHING:
        check.problems.append(f"{network} exchanges messages; it does not publish posts")
        return check
    if not network_verified(network):
        check.problems.append(f"{network} publishing rules are not verified yet")
        return check
    if network == "instagram":
        if media_type == "text":
            check.problems.append("instagram needs an image or a video [IG-JPEG]")
        if media_type in ("image", "carousel") and fmt not in ("jpg", "jpeg"):
            check.problems.append("instagram images must be JPEG [IG-JPEG]")
        if not public_url:
            check.problems.append("instagram media must be on a public URL [IG-PUBLIC-URL]")
        if posts_last_24h >= IG_DAILY_POST_LIMIT:
            check.problems.append("instagram allows 100 API posts per 24 hours [IG-LIMIT]")
    if network == "tiktok":
        if media_type == "video" and fmt != "mp4":
            check.problems.append("tiktok videos must be MP4 with H.264 [TT-MEDIA]")
        if media_type == "image" and not public_url:
            check.problems.append("tiktok photos must come from a verified-domain URL [TT-MEDIA]")
        if media_type in ("text", "carousel"):
            check.problems.append("tiktok publishes a video or photos [TT-MEDIA]")
        if not audited:
            check.visibility = "private"  # [TT-UNAUDITED]: not an error, but never public
    return check


def check_whatsapp_message(
    *,
    opted_in: bool,
    window_open: bool,
    template_status: str | None,
    human_handoff: bool,
) -> list[str]:
    """Problems with sending one WhatsApp message; empty means it may be sent."""
    problems = []
    if not opted_in:
        problems.append("the person has not opted in [WA-OPT-IN]")
    if not window_open and template_status != "APPROVED":
        problems.append(
            "outside the service window only an approved template may be sent [WA-WINDOW]"
        )
    if not human_handoff:
        problems.append("a path to a human must be available [WA-HUMAN]")
    return problems


# --- accounts ---------------------------------------------------------------------------

channel_accounts = Table(
    "channel_accounts",
    metadata,
    Column("tenant", String(64), primary_key=True),
    Column("account_id", String(32), primary_key=True),
    Column("network", String(16), nullable=False),
    # The platform's id for the page, number or profile (not a person's data).
    Column("external_id", String(128), nullable=False),
    Column("handle", String(120), nullable=False),
    # Which professional of the establishment the account belongs to; null = the practice.
    Column("professional_id", String(64), nullable=True),
    Column("secret_ref", String(64), nullable=False),
    # TikTok: whether the API client passed TikTok's audit (unaudited posts are private).
    Column("audited", Boolean, nullable=False, default=False),
    Column("active", Boolean, nullable=False, default=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("created_by", String(128), nullable=False),
    Index("ix_channel_accounts_lookup", "tenant", "network", "external_id"),
)


class AccountConflict(ValueError):
    """The same page, number or profile is already connected and active."""


def secret_configured(ref: str) -> bool:
    return bool(os.environ.get(CREDENTIAL_ENV_PREFIX + ref))


def resolve_secret(ref: str) -> str | None:
    """The credential for an account, for the adapters only. Never logged or returned."""
    if not re.fullmatch(REF_PATTERN, ref):
        return None
    return os.environ.get(CREDENTIAL_ENV_PREFIX + ref) or None


def _account(row: Any) -> dict[str, Any]:
    return {
        "account_id": row.account_id,
        "network": row.network,
        "external_id": row.external_id,
        "handle": row.handle,
        "professional_id": row.professional_id,
        "secret_ref": row.secret_ref,
        "secret_configured": secret_configured(row.secret_ref),
        "audited": row.audited,
        "active": row.active,
        "created_at": row.created_at,
    }


class SocialAccounts:
    def __init__(
        self, db: Database, audit: AuditLog, professionals: Professionals | None = None
    ) -> None:
        self.db = db
        self.audit = audit
        # An account given to a professional must name a registered, active one.
        self.professionals = professionals

    async def add(
        self,
        tenant: str,
        actor: str,
        *,
        network: Network,
        external_id: str,
        handle: str,
        secret_ref: str,
        professional_id: str | None = None,
        audited: bool = False,
    ) -> dict[str, Any]:
        if not re.fullmatch(REF_PATTERN, secret_ref):
            raise ValueError("secret_ref must be an upper-case name such as WA_CLINIC_MAIN")
        if professional_id is not None and self.professionals is not None:
            await self.professionals.require_active(tenant, professional_id)
        account_id = uuid.uuid4().hex[:12]
        same = and_(
            channel_accounts.c.tenant == tenant,
            channel_accounts.c.network == network,
            channel_accounts.c.external_id == external_id,
            channel_accounts.c.active.is_(True),
        )
        async with self.db.engine.begin() as conn:
            if (await conn.execute(select(channel_accounts.c.account_id).where(same))).first():
                raise AccountConflict(f"this {network} account is already connected")
            await conn.execute(
                insert(channel_accounts).values(
                    tenant=tenant,
                    account_id=account_id,
                    network=network,
                    external_id=external_id,
                    handle=handle,
                    professional_id=professional_id,
                    secret_ref=secret_ref,
                    audited=audited,
                    active=True,
                    created_at=utcnow(),
                    created_by=actor,
                )
            )
            await self.audit.record_in(
                conn,
                tenant,
                actor,
                "channel_account.connected",
                f"channel_account/{account_id}",
                details={"network": network, "professional_id": professional_id},
            )
        return await self.get(tenant, account_id)

    async def get(self, tenant: str, account_id: str) -> dict[str, Any]:
        query = select(channel_accounts).where(
            and_(channel_accounts.c.tenant == tenant, channel_accounts.c.account_id == account_id)
        )
        async with self.db.engine.connect() as conn:
            row = (await conn.execute(query)).first()
        if row is None:
            raise KeyError(account_id)
        return _account(row)

    async def list(self, tenant: str, professional_id: str | None = None) -> list[dict[str, Any]]:
        query = select(channel_accounts).where(channel_accounts.c.tenant == tenant)
        if professional_id is not None:
            query = query.where(channel_accounts.c.professional_id == professional_id)
        query = query.order_by(channel_accounts.c.created_at, channel_accounts.c.account_id)
        async with self.db.engine.connect() as conn:
            return [_account(r) for r in await conn.execute(query)]

    async def disable(self, tenant: str, account_id: str, actor: str) -> bool:
        query = (
            update(channel_accounts)
            .where(
                and_(
                    channel_accounts.c.tenant == tenant,
                    channel_accounts.c.account_id == account_id,
                    channel_accounts.c.active.is_(True),
                )
            )
            .values(active=False)
        )
        async with self.db.engine.begin() as conn:
            done = (await conn.execute(query)).rowcount == 1
            if done:
                await self.audit.record_in(
                    conn, tenant, actor, "channel_account.disabled", f"channel_account/{account_id}"
                )
        return done
