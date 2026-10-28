"""OAuth grants sealed at rest, bound to tenant, reference, network and account.

Legacy environment references still work. OAuth references never fall back to the
environment or to a dry run when the grant is missing, expired or fails integrity.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import Column, DateTime, String, Table, Text, UniqueConstraint, and_, delete, select

from orchestrator.config import Settings
from orchestrator.db import Database, aware, metadata, utcnow
from orchestrator.sealing import Sealer, SealError

credentials = Table(
    "social_credentials",
    metadata,
    Column("tenant", String(64), primary_key=True),
    Column("secret_ref", String(64), primary_key=True),
    Column("network", String(16), nullable=False),
    Column("external_id", String(128), nullable=False),
    Column("token_sealed", Text, nullable=False),
    Column("expires_at", DateTime(timezone=True), nullable=True),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("network", "external_id", name="uq_social_oauth_asset"),
)


class ReconnectRequired(ValueError):
    """The account needs fresh authorization; it must not simulate a successful send."""


class SocialCredentials:
    def __init__(self, db: Database, settings: Settings) -> None:
        self.db = db
        self.sealer = Sealer(settings.pseudonym_secret(), b"social-oauth-credentials-v1")

    @staticmethod
    def binding(tenant: str, ref: str, network: str, external_id: str) -> str:
        return f"{tenant}/{ref}/{network}/{external_id}"

    async def resolve(self, tenant: str, ref: str, network: str, external_id: str) -> str | None:
        from orchestrator.social import channel_accounts, resolve_secret

        if not ref.startswith("OAUTH_"):
            return resolve_secret(ref)
        async with self.db.engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        select(credentials)
                        .join(
                            channel_accounts,
                            and_(
                                channel_accounts.c.tenant == credentials.c.tenant,
                                channel_accounts.c.secret_ref == credentials.c.secret_ref,
                                channel_accounts.c.network == credentials.c.network,
                                channel_accounts.c.external_id == credentials.c.external_id,
                                channel_accounts.c.active.is_(True),
                            ),
                        )
                        .where(
                            and_(
                                credentials.c.tenant == tenant,
                                credentials.c.secret_ref == ref,
                                credentials.c.network == network,
                                credentials.c.external_id == external_id,
                            )
                        )
                    )
                )
                .mappings()
                .first()
            )
        expiry = aware(row["expires_at"]) if row else None
        if row is None or (expiry is not None and expiry <= utcnow()):
            raise ReconnectRequired("social authorization needs reconnection")
        try:
            return self.sealer.open(
                self.binding(tenant, ref, row["network"], row["external_id"]), row["token_sealed"]
            )
        except SealError as exc:
            raise ReconnectRequired("social authorization needs reconnection") from exc

    async def configured(self, tenant: str, ref: str, network: str, external_id: str) -> bool:
        try:
            return bool(await self.resolve(tenant, ref, network, external_id))
        except ReconnectRequired:
            return False

    async def remove_in(self, conn: Any, tenant: str, ref: str) -> None:
        await conn.execute(
            delete(credentials).where(
                and_(credentials.c.tenant == tenant, credentials.c.secret_ref == ref)
            )
        )
