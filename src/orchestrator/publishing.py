"""Publications on social networks (ADR 0015, M3): a creative and its caption, approved by a
person, then published once.

Lifecycle: pending_approval -> approved -> publishing -> published | failed | uncertain,
or cancelled before publishing. What is approved cannot change: the rendered file is stored
with its SHA-256 and there is no edit; to change anything, cancel and create another.

* The caption and the creative pass the pack's copy rules before anything is stored.
* An approval needs the owner too when a discount exceeds the pack's cap (as campaigns do).
* Publishing claims the row atomically (approved -> publishing), so it happens once.
* Without the account's secret the publication runs dry: recorded, never sent.
* A transport error after the request may have reached the platform: `uncertain`, never
  retried automatically (as the campaign outbox does).
* Every step is in the hash-chained audit log."""

from __future__ import annotations

import tempfile
import uuid
from pathlib import Path
from typing import Any, Literal

import httpx
from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    String,
    Table,
    and_,
    insert,
    select,
    update,
)

from orchestrator import creatives
from orchestrator.config import Settings
from orchestrator.db import Database, metadata, utcnow
from orchestrator.governance import AuditLog
from orchestrator.media_store import MediaStore, object_name
from orchestrator.packs import pack_for
from orchestrator.publishers import InstagramPublisher, PublishError, PublishResult, TikTokPublisher
from orchestrator.risk import check_copy
from orchestrator.social import PUBLISHING, SocialAccounts, check_publish, resolve_secret

Kind = Literal["infographic", "video"]
MAX_CAPTION = 2200  # TikTok's title limit; Instagram's caption limit is the same size

publications = Table(
    "publications",
    metadata,
    Column("tenant", String(64), primary_key=True),
    Column("publication_id", String(32), primary_key=True),
    Column("account_id", String(32), nullable=False),
    Column("network", String(16), nullable=False),
    Column("media_type", String(8), nullable=False),
    Column("media_format", String(8), nullable=False),
    Column("object_name", String(160), nullable=False),
    Column("sha256", String(64), nullable=False),
    Column("caption", String(MAX_CAPTION), nullable=False),
    Column("brief", JSON, nullable=False),
    Column("status", String(20), nullable=False),
    Column("needs_owner_approval", Boolean, nullable=False),
    Column("mode", String(8), nullable=True),
    Column("visibility", String(8), nullable=True),
    Column("external_id", String(128), nullable=True),
    Column("error", String(300), nullable=True),
    Column("created_by", String(128), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("approved_by", String(128), nullable=True),
    Column("approved_at", DateTime(timezone=True), nullable=True),
    Column("published_at", DateTime(timezone=True), nullable=True),
)


class PublicationError(ValueError):
    """The request does not fit the account, the network or the publication's state."""


class OwnerApprovalRequired(PermissionError):
    """The creative or caption has a discount above the pack's cap."""


def _row(row: Any) -> dict[str, Any]:
    return {
        c.name: getattr(row, c.name) for c in publications.c if c.name not in ("tenant", "brief")
    }


class PublicationService:
    def __init__(
        self,
        db: Database,
        audit: AuditLog,
        social: SocialAccounts,
        store: MediaStore,
        settings: Settings,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.db = db
        self.audit = audit
        self.social = social
        self.store = store
        self.settings = settings
        self.transport = transport  # tests replace the network

    def _where(self, tenant: str, publication_id: str, *extra: Any) -> Any:
        return and_(
            publications.c.tenant == tenant,
            publications.c.publication_id == publication_id,
            *extra,
        )

    async def get(self, tenant: str, publication_id: str) -> dict[str, Any]:
        async with self.db.engine.connect() as conn:
            row = (
                await conn.execute(select(publications).where(self._where(tenant, publication_id)))
            ).first()
        if row is None:
            raise KeyError(publication_id)
        return _row(row)

    async def list(self, tenant: str) -> list[dict[str, Any]]:
        query = (
            select(publications)
            .where(publications.c.tenant == tenant)
            .order_by(publications.c.created_at.desc(), publications.c.publication_id)
        )
        async with self.db.engine.connect() as conn:
            return [_row(r) for r in await conn.execute(query)]

    async def create(
        self,
        tenant: str,
        actor: str,
        *,
        account_id: str,
        kind: Kind,
        brief: creatives.Brief,
        caption: str,
        fmt: creatives.Format | None = None,
    ) -> dict[str, Any]:
        account = await self.social.get(tenant, account_id)  # KeyError: not this tenant's
        network = account["network"]
        if not account["active"]:
            raise PublicationError("the account is disabled")
        if network not in PUBLISHING:
            raise PublicationError(f"{network} does not publish posts")
        if network == "tiktok" and kind != "video":
            raise PublicationError("tiktok photos need a verified domain; publish a video")
        if not caption.strip() or len(caption) > MAX_CAPTION:
            raise PublicationError(f"the caption needs 1 to {MAX_CAPTION} characters")
        pack = pack_for(self.settings, tenant)
        caption_check = check_copy(caption, pack)
        if not caption_check.ok:
            raise creatives.CreativeRejected(caption_check.violations)

        publication_id = uuid.uuid4().hex[:12]
        suffix = ".jpg" if kind == "infographic" else ".mp4"
        name = object_name(tenant, publication_id, suffix)
        with tempfile.TemporaryDirectory(prefix="publication-") as tmp:
            out = Path(tmp) / f"creative{suffix}"
            if kind == "infographic":
                made = creatives.render_infographic(brief, pack, out, fmt or "feed")
            else:
                made = creatives.render_video(brief, pack, out, self.settings, fmt or "story")
            # Format and type only here; the URL and the audit status are checked at publish.
            shape = check_publish(
                network, media_type=made.media_type, media_format=made.media_format, public_url=True
            )
            if not shape.allowed:
                raise PublicationError("; ".join(shape.problems))
            content_type = "image/jpeg" if made.media_format == "jpeg" else "video/mp4"
            await self.store.put(name, out, content_type)

        values = {
            "tenant": tenant,
            "publication_id": publication_id,
            "account_id": account_id,
            "network": network,
            "media_type": made.media_type,
            "media_format": made.media_format,
            "object_name": name,
            "sha256": made.sha256,
            "caption": caption,
            "brief": brief.model_dump(),
            "status": "pending_approval",
            "needs_owner_approval": made.needs_owner_approval or caption_check.needs_owner_approval,
            "created_by": actor,
            "created_at": utcnow(),
        }
        async with self.db.engine.begin() as conn:
            await conn.execute(insert(publications).values(**values))
            await self.audit.record_in(
                conn,
                tenant,
                actor,
                "publication.created",
                f"publication/{publication_id}",
                details={"network": network, "sha256": made.sha256},
            )
        return await self.get(tenant, publication_id)

    async def _transition(
        self,
        tenant: str,
        publication_id: str,
        actor: str,
        *,
        frm: tuple[str, ...],
        values: dict[str, Any],
        action: str,
    ) -> bool:
        query = (
            update(publications)
            .where(self._where(tenant, publication_id, publications.c.status.in_(frm)))
            .values(**values)
        )
        async with self.db.engine.begin() as conn:
            done = (await conn.execute(query)).rowcount == 1
            if done:
                await self.audit.record_in(
                    conn, tenant, actor, action, f"publication/{publication_id}"
                )
        return done

    async def approve(
        self, tenant: str, publication_id: str, actor: str, *, owner_approval: bool
    ) -> dict[str, Any]:
        current = await self.get(tenant, publication_id)
        if current["needs_owner_approval"] and not owner_approval:
            raise OwnerApprovalRequired("a discount above the pack's cap needs the owner")
        done = await self._transition(
            tenant,
            publication_id,
            actor,
            frm=("pending_approval",),
            values={"status": "approved", "approved_by": actor, "approved_at": utcnow()},
            action="publication.approved",
        )
        if not done:
            raise PublicationError(f"cannot approve a publication that is {current['status']}")
        return await self.get(tenant, publication_id)

    async def cancel(self, tenant: str, publication_id: str, actor: str) -> dict[str, Any]:
        current = await self.get(tenant, publication_id)
        done = await self._transition(
            tenant,
            publication_id,
            actor,
            frm=("pending_approval", "approved"),
            values={"status": "cancelled"},
            action="publication.cancelled",
        )
        if not done:
            raise PublicationError(f"cannot cancel a publication that is {current['status']}")
        return await self.get(tenant, publication_id)

    async def publish(self, tenant: str, publication_id: str, actor: str) -> dict[str, Any]:
        current = await self.get(tenant, publication_id)
        claimed = await self._transition(
            tenant,
            publication_id,
            actor,
            frm=("approved",),
            values={"status": "publishing"},
            action="publication.publishing",
        )
        if not claimed:
            raise PublicationError(f"cannot publish a publication that is {current['status']}")
        status, values = await self._send(tenant, current)
        async with self.db.engine.begin() as conn:
            await conn.execute(
                update(publications)
                .where(self._where(tenant, publication_id, publications.c.status == "publishing"))
                .values(status=status, **values)
            )
            await self.audit.record_in(
                conn,
                tenant,
                actor,
                f"publication.{status}",
                f"publication/{publication_id}",
                details={"network": current["network"], "mode": values.get("mode")},
            )
        return await self.get(tenant, publication_id)

    async def _send(self, tenant: str, pub: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        account = await self.social.get(tenant, pub["account_id"])
        url = (
            await self.store.signed_url(pub["object_name"])
            if pub["network"] == "instagram"
            else None
        )
        check = check_publish(
            pub["network"],
            media_type=pub["media_type"],
            media_format=pub["media_format"],
            public_url=url is not None or pub["network"] != "instagram",
            audited=account["audited"],
        )
        token = resolve_secret(account["secret_ref"]) if account["active"] else None
        if token is None:  # no credential: record what would happen, send nothing
            return "published", {
                "mode": "dry_run",
                "visibility": check.visibility,
                "external_id": f"dry-run-{pub['publication_id']}",
                "error": "; ".join(check.problems)[:300] or None,
                "published_at": utcnow(),
            }
        if not check.allowed:
            return "failed", {"mode": "live", "error": "; ".join(check.problems)[:300]}
        try:
            result = await self._live(pub, account, token, url)
        except PublishError as exc:
            return "failed", {"mode": "live", "error": str(exc)[:300]}
        except httpx.TransportError as exc:
            return "uncertain", {"mode": "live", "error": f"transport: {type(exc).__name__}"}
        return "published", {
            "mode": result.mode,
            "visibility": result.visibility,
            "external_id": result.external_id,
            "published_at": utcnow(),
        }

    async def _live(
        self, pub: dict[str, Any], account: dict[str, Any], token: str, url: str | None
    ) -> PublishResult:
        async with httpx.AsyncClient(
            timeout=self.settings.publish_timeout_seconds, transport=self.transport
        ) as client:
            if pub["network"] == "instagram":
                if url is None:  # the pre-flight check already refuses this
                    raise PublishError("instagram: no media URL")
                return await InstagramPublisher(
                    client,
                    self.settings.meta_graph_base,
                    poll_attempts=self.settings.publish_poll_attempts,
                    poll_seconds=self.settings.publish_poll_seconds,
                ).publish(
                    ig_user_id=account["external_id"],
                    token=token,
                    media_type=pub["media_type"],
                    media_url=url,
                    caption=pub["caption"],
                )
            video = self.store.local_path(pub["object_name"])
            if video is None:
                raise PublishError("tiktok: the video is not available locally")
            return await TikTokPublisher(client, self.settings.tiktok_api_base).publish(
                token=token, video=video, caption=pub["caption"], audited=account["audited"]
            )
