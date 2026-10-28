"""Durable, browser-bound social authorization and explicit asset selection.

The code callback only prepares an encrypted, short-lived choice. Connecting still
requires the same authenticated staff member and tenant, with current permissions.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import uuid
from dataclasses import asdict
from datetime import datetime, timedelta
from typing import Any, cast

from sqlalchemy import Column, DateTime, String, Table, Text, and_, delete, insert, select, update
from sqlalchemy.exc import IntegrityError

from orchestrator.auth import Principal
from orchestrator.config import Settings
from orchestrator.db import Database, metadata, utcnow
from orchestrator.governance import AuditLog
from orchestrator.meta_login import PROVIDERS, Asset, LoginError, MetaLogin, Provider
from orchestrator.sealing import Sealer
from orchestrator.social import AccountConflict, SocialAccounts, channel_accounts
from orchestrator.social_credentials import credentials

flows = Table(
    "social_login_flows",
    metadata,
    Column("tenant", String(64), primary_key=True),
    Column("flow_id", String(32), primary_key=True, unique=True),
    Column("actor", String(128), nullable=False),
    Column("network", String(16), nullable=False),
    Column("professional_id", String(64), nullable=True),
    Column("state_hash", String(64), nullable=False, unique=True),
    Column("binding_hash", String(64), nullable=False),
    Column("status", String(16), nullable=False),
    Column("payload_sealed", Text, nullable=True),
    Column("error_code", String(40), nullable=True),
    Column("account_id", String(32), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("expires_at", DateTime(timezone=True), nullable=False),
)


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class SocialLogin:
    def __init__(
        self, db: Database, audit: AuditLog, social: SocialAccounts, settings: Settings
    ) -> None:
        self.db, self.audit, self.social, self.settings = db, audit, social, settings
        self.provider = MetaLogin(settings)
        self.sealer = Sealer(settings.pseudonym_secret(), b"social-oauth-flow-v1")

    def configuration(self) -> dict[str, Any]:
        return {"providers": {p: {"available": self.provider.configured(p)} for p in PROVIDERS}}

    async def purge(self, *, dry_run: bool = False) -> int:
        async with self.db.engine.begin() as conn:
            where = flows.c.expires_at <= utcnow()
            count = len((await conn.execute(select(flows.c.flow_id).where(where))).all())
            if not dry_run:
                await conn.execute(delete(flows).where(where))
        return count

    async def start(
        self,
        p: Principal,
        provider: Provider,
        cookie: str | None,
        professional_id: str | None = None,
    ) -> tuple[dict[str, Any], str]:
        if not self.provider.configured(provider):
            raise LoginError("setup_required")
        if professional_id and self.social.professionals:
            await self.social.professionals.require_active(p.tenant, professional_id)
        await self.purge()
        binding = cookie if cookie and len(cookie) == 64 else secrets.token_hex(32)
        state, flow_id = secrets.token_urlsafe(32), uuid.uuid4().hex
        async with self.db.engine.begin() as conn:
            pending = (
                await conn.execute(
                    select(flows.c.flow_id)
                    .where(
                        and_(
                            flows.c.tenant == p.tenant,
                            flows.c.actor == p.id,
                            flows.c.status.in_(["pending", "exchanging", "ready", "connecting"]),
                        )
                    )
                    .limit(5)
                )
            ).all()
            if len(pending) >= 5:
                raise LoginError("too_many_attempts")
            await conn.execute(
                insert(flows).values(
                    flow_id=flow_id,
                    tenant=p.tenant,
                    actor=p.id,
                    network=provider,
                    professional_id=professional_id,
                    state_hash=digest(state),
                    binding_hash=digest(binding),
                    status="pending",
                    created_at=utcnow(),
                    expires_at=utcnow() + timedelta(minutes=15),
                )
            )
        url = (
            "/social-connect#" + flow_id + "." + state
            if provider == "whatsapp"
            else self.provider.authorize_url(provider, state)
        )
        return {"flow_id": flow_id, "authorization_url": url}, binding

    async def launch(self, flow_id: str, state: str, cookie: str) -> dict[str, str]:
        async with self.db.engine.connect() as conn:
            row = (
                await conn.execute(
                    select(flows.c.flow_id).where(
                        and_(
                            flows.c.flow_id == flow_id,
                            flows.c.state_hash == digest(state),
                            flows.c.binding_hash == digest(cookie),
                            flows.c.network == "whatsapp",
                            flows.c.status == "pending",
                            flows.c.expires_at > utcnow(),
                        )
                    )
                )
            ).first()
        if row is None or not self.provider.configured("whatsapp"):
            raise LoginError("invalid_or_expired")
        return {
            "app_id": self.settings.meta_app_id,
            "config_id": self.settings.meta_whatsapp_config_id,
            "version": self.settings.social_login_graph_version,
        }

    async def complete(
        self, provider: Provider, state: str, cookie: str, code: str | None, *, denied: bool = False
    ) -> None:
        # A conditional claim consumes state once, across replicas, before provider I/O.
        async with self.db.engine.begin() as conn:
            row = (
                (
                    await conn.execute(
                        select(flows)
                        .where(
                            and_(
                                flows.c.network == provider,
                                flows.c.state_hash == digest(state),
                                flows.c.binding_hash == digest(cookie),
                                flows.c.expires_at > utcnow(),
                                flows.c.status == "pending",
                            )
                        )
                        .with_for_update()
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                raise LoginError("invalid_or_expired")
            claimed = await conn.execute(
                update(flows)
                .where(
                    and_(
                        flows.c.flow_id == row["flow_id"],
                        flows.c.status == "pending",
                    )
                )
                .values(status="exchanging")
            )
            if claimed.rowcount != 1:
                raise LoginError("invalid_or_expired")
        fid = row["flow_id"]
        try:
            if denied or not code:
                raise LoginError("authorization_cancelled")
            assets = await self.provider.exchange(provider, code)
            if not assets:
                raise LoginError("no_accounts")
            payload = [{"choice": uuid.uuid4().hex, **asdict(asset)} for asset in assets]
            sealed = self.sealer.seal(row["tenant"] + "/" + fid, json.dumps(payload, default=str))
            async with self.db.engine.begin() as conn:
                await conn.execute(
                    update(flows)
                    .where(
                        and_(
                            flows.c.flow_id == fid,
                            flows.c.status == "exchanging",
                            flows.c.expires_at > utcnow(),
                        )
                    )
                    .values(status="ready", payload_sealed=sealed)
                )
        except LoginError as exc:
            await self._fail(fid, str(exc))
        except (ValueError, TypeError, KeyError):
            await self._fail(fid, "provider_response_invalid")

    async def _fail(self, flow_id: str, error: str) -> None:
        async with self.db.engine.begin() as conn:
            await conn.execute(
                update(flows)
                .where(flows.c.flow_id == flow_id)
                .values(status="failed", payload_sealed=None, error_code=error)
            )

    async def status(self, p: Principal, flow_id: str) -> dict[str, Any]:
        async with self.db.engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        select(flows).where(
                            and_(
                                flows.c.flow_id == flow_id,
                                flows.c.tenant == p.tenant,
                                flows.c.actor == p.id,
                                flows.c.expires_at > utcnow(),
                            )
                        )
                    )
                )
                .mappings()
                .first()
            )
        if row is None:
            raise LoginError("invalid_or_expired")
        choices = self._payload(row) if row["status"] == "ready" else []
        return {
            "flow_id": flow_id,
            "status": row["status"],
            "network": row["network"],
            "error_code": row["error_code"],
            "account_id": row["account_id"],
            "choices": [{k: a[k] for k in ("choice", "network", "handle")} for a in choices],
        }

    def _payload(self, row: Any) -> list[dict[str, Any]]:
        return cast(
            list[dict[str, Any]],
            json.loads(
                self.sealer.open(row["tenant"] + "/" + row["flow_id"], row["payload_sealed"])
            ),
        )

    async def connect(self, p: Principal, flow_id: str, choice: str) -> dict[str, Any]:
        if not self.settings.social_login_enabled:
            raise LoginError("setup_required")
        async with self.db.engine.begin() as conn:
            row = (
                (
                    await conn.execute(
                        select(flows)
                        .where(
                            and_(
                                flows.c.flow_id == flow_id,
                                flows.c.tenant == p.tenant,
                                flows.c.actor == p.id,
                                flows.c.status == "ready",
                                flows.c.expires_at > utcnow(),
                            )
                        )
                        .with_for_update()
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                raise LoginError("invalid_or_expired")
            selected = next((a for a in self._payload(row) if a["choice"] == choice), None)
            if selected is None:
                raise LoginError("invalid_choice")
            data = {k: v for k, v in selected.items() if k != "choice"}
            data["expires_at"] = (
                datetime.fromisoformat(data["expires_at"]) if data.get("expires_at") else None
            )
            asset = Asset(**data)
            if asset.expires_at and asset.expires_at <= utcnow():
                raise LoginError("authorization_expired")
            if row["professional_id"] and self.social.professionals:
                await self.social.professionals.require_active(p.tenant, row["professional_id"])
            changed = await conn.execute(
                update(flows)
                .where(and_(flows.c.flow_id == flow_id, flows.c.status == "ready"))
                .values(status="connecting")
            )
            if changed.rowcount != 1:
                raise LoginError("invalid_or_expired")
        try:
            await self.provider.subscribe(asset)
            account_id = await self._store(p, row, asset)
        except AccountConflict:
            await self._fail(flow_id, "account_in_use")
            raise LoginError("account_in_use") from None
        except IntegrityError:
            await self._fail(flow_id, "account_in_use")
            raise LoginError("account_in_use") from None
        except LoginError as exc:
            await self._fail(flow_id, str(exc))
            raise
        return await self.social.get(p.tenant, account_id)

    async def _store(self, p: Principal, flow: Any, asset: Asset) -> str:
        async with self.db.engine.begin() as conn:
            others = (
                await conn.execute(
                    select(channel_accounts)
                    .where(
                        and_(
                            channel_accounts.c.network == asset.network,
                            channel_accounts.c.external_id == asset.external_id,
                            channel_accounts.c.active.is_(True),
                        )
                    )
                    .with_for_update()
                )
            ).all()
            if any(a.tenant != p.tenant for a in others):
                raise AccountConflict("account_in_use")
            old = next((a for a in others if a.tenant == p.tenant), None)
            account_id = old.account_id if old else uuid.uuid4().hex
            if old and old.secret_ref.startswith("OAUTH_"):
                await self.social.credentials.remove_in(conn, p.tenant, old.secret_ref)
            ref = "OAUTH_" + uuid.uuid4().hex.upper()
            await conn.execute(
                insert(credentials).values(
                    tenant=p.tenant,
                    secret_ref=ref,
                    network=asset.network,
                    external_id=asset.external_id,
                    token_sealed=self.social.credentials.sealer.seal(
                        self.social.credentials.binding(
                            p.tenant, ref, asset.network, asset.external_id
                        ),
                        asset.token,
                    ),
                    expires_at=asset.expires_at,
                    updated_at=utcnow(),
                )
            )
            values = dict(
                handle=asset.handle,
                secret_ref=ref,
                active=True,
                professional_id=flow["professional_id"],
            )
            if old:
                await conn.execute(
                    update(channel_accounts)
                    .where(
                        and_(
                            channel_accounts.c.tenant == p.tenant,
                            channel_accounts.c.account_id == account_id,
                        )
                    )
                    .values(**values)
                )
            else:
                await conn.execute(
                    insert(channel_accounts).values(
                        tenant=p.tenant,
                        account_id=account_id,
                        network=asset.network,
                        external_id=asset.external_id,
                        audited=False,
                        created_at=utcnow(),
                        created_by=p.id,
                        **values,
                    )
                )
            await conn.execute(
                update(flows)
                .where(flows.c.flow_id == flow["flow_id"])
                .values(status="connected", payload_sealed=None, account_id=account_id)
            )
            await self.audit.record_in(
                conn,
                p.tenant,
                p.id,
                "channel_account.authorized",
                f"channel_account/{account_id}",
                details={"network": asset.network, "method": "oauth"},
            )
        return account_id
