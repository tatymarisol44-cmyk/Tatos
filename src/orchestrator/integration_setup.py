"""Tenant-owned onboarding references, separate from verified provider credentials.

No network transport is defined here: the two vendors' API contracts have not been
provided. A declared RUC/account never grants a Meta role or proves asset ownership.
"""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Column, DateTime, Integer, String, Table, Text, insert, select, update
from sqlalchemy.exc import IntegrityError

from orchestrator.auth import Principal
from orchestrator.config import Settings
from orchestrator.db import Database, metadata, utcnow
from orchestrator.governance import AuditLog
from orchestrator.sealing import Sealer

Provider = Literal["meta", "tufacturero", "mentaltestlab"]
PROVIDERS: tuple[Provider, ...] = ("meta", "tufacturero", "mentaltestlab")
CATALOG = {
    "meta": {
        "name": "Facebook, Instagram y WhatsApp",
        "url": "https://developers.facebook.com/apps/",
        "requirements": [
            "Iniciar sesión en Meta y autorizar los activos del consultorio.",
            "En desarrollo, la cuenta de prueba necesita un rol admitido por Meta.",
            "Para clientes externos, completar la habilitación y permisos que Meta exija.",
        ],
    },
    "tufacturero": {
        "name": "TuFacturero",
        "url": "https://tufacturero.ec/",
        "requirements": [
            "Obtener de TuFacturero el contrato de su API y acceso al ambiente de pruebas.",
            "Confirmar autenticación, emisión, consulta de estados y manejo de duplicados.",
            "Configurar privadamente las credenciales y validar el emisor con el proveedor.",
        ],
    },
    "mentaltestlab": {
        "name": "MentalTestLab",
        "url": "https://mentaltestlab.com/evaluaciones/",
        "requirements": [
            "Confirmar con MentalTestLab si ofrece API para esta integración.",
            "Obtener documentación, acceso de pruebas y autorización de uso de evaluaciones.",
            "Definir con el proveedor permisos y tratamiento de datos antes de sincronizar.",
        ],
    },
}

profiles = Table(
    "integration_setup_profiles",
    metadata,
    Column("tenant", String(64), primary_key=True),
    Column("provider", String(24), primary_key=True),
    Column("version", Integer, nullable=False),
    Column("profile_sealed", Text, nullable=True),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)


class SetupConflict(ValueError):
    """Reload after a concurrent change; no stale write or clear may land."""


class SetupRejected(ValueError):
    """A provider's references do not match the documented onboarding fields."""


class SetupProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    ruc: str | None = Field(default=None, pattern=r"^[0-9]{13}$")
    account_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_.-]{1,128}$")

    def for_provider(self, provider: Provider) -> None:
        if provider in {"meta", "tufacturero"} and self.ruc is None:
            raise SetupRejected("ruc_required")
        if provider == "meta" and (
            self.account_id is None
            or not self.account_id.isascii()
            or not self.account_id.isdigit()
        ):
            raise SetupRejected("meta_business_id_required")
        if provider == "mentaltestlab" and (self.ruc is not None or self.account_id is None):
            raise SetupRejected("mentaltestlab_account_reference_required")


class IntegrationSetup:
    def __init__(self, db: Database, audit: AuditLog, settings: Settings) -> None:
        self.db, self.audit = db, audit
        self.sealer = Sealer(settings.pseudonym_secret(), b"integration-setup-profiles-v1")

    @staticmethod
    def binding(tenant: str, provider: Provider) -> str:
        return f"{tenant}/{provider}"

    def view(self, tenant: str, provider: Provider, row: Any = None) -> dict[str, Any]:
        sealed = row["profile_sealed"] if row else None
        profile = (
            json.loads(self.sealer.open(self.binding(tenant, provider), sealed)) if sealed else None
        )
        return {
            "provider": provider,
            **CATALOG[provider],
            "version": row["version"] if row else 0,
            "profile": profile,
            "configured": profile is not None,
            "status": "references_saved" if profile is not None else "not_configured",
            "identity_verified": False,
            "external_connection": "not_checked",
            "api_transport_implemented": provider == "meta",
            "requirements": CATALOG[provider]["requirements"],
        }

    async def listing(self, tenant: str) -> list[dict[str, Any]]:
        async with self.db.engine.connect() as conn:
            rows = (
                (await conn.execute(select(profiles).where(profiles.c.tenant == tenant)))
                .mappings()
                .all()
            )
        by_provider = {row["provider"]: row for row in rows}
        return [self.view(tenant, provider, by_provider.get(provider)) for provider in PROVIDERS]

    async def save(
        self, p: Principal, provider: Provider, version: int, profile: SetupProfile | None
    ) -> dict[str, Any]:
        if profile is not None:
            profile.for_provider(provider)
        sealed = (
            self.sealer.seal(self.binding(p.tenant, provider), profile.model_dump_json())
            if profile is not None
            else None
        )
        values = {
            "version": version + 1,
            "profile_sealed": sealed,
            "updated_at": utcnow(),
        }
        try:
            async with self.db.engine.begin() as conn:
                if version == 0:
                    await conn.execute(
                        insert(profiles).values(tenant=p.tenant, provider=provider, **values)
                    )
                else:
                    changed = await conn.execute(
                        update(profiles)
                        .where(
                            profiles.c.tenant == p.tenant,
                            profiles.c.provider == provider,
                            profiles.c.version == version,
                        )
                        .values(**values)
                    )
                    if changed.rowcount != 1:
                        raise SetupConflict("profile_changed_reload")
                await self.audit.record_in(
                    conn,
                    p.tenant,
                    p.id,
                    "integration.references.saved" if profile else "integration.references.cleared",
                    f"integration_setup/{provider}",
                )
        except IntegrityError as exc:
            raise SetupConflict("profile_changed_reload") from exc
        # Keep a versioned tombstone when clearing, preventing stale recreation (ABA).
        return self.view(p.tenant, provider, values)
