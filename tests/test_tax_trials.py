"""Non-fiscal trials: synthetic identities, no network, no SRI authorisation."""

import asyncio
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import update

from orchestrator.auth import Principal
from orchestrator.billing import ChargeKind
from orchestrator.billing_store import BillingStore
from orchestrator.db import utcnow
from orchestrator.tax_trials import IssuerProfile, TaxTrialError, TaxTrials, TrialRequest, documents
from tests.test_billing_store import billing as billing

PROFILE = {
    "ruc": "0000000000001",
    "legal_name": "Emisor ficticio QA",
    "trade_name": "Clínica inventada",
    "address": "Dirección ficticia",
    "establishment": "001",
    "emission_point": "001",
    "tax_regime": "Declarado para prueba, no verificado",
}


async def setup(
    billing: BillingStore, scenario: str = "accept"
) -> tuple[TaxTrials, Principal, dict[str, Any]]:
    service = TaxTrials(billing.db, billing.audit, "test")
    p = Principal("acme", "owner", "staff", frozenset({"owner"}))
    issuer = await service.configure(p, IssuerProfile.model_validate(PROFILE), None, 0)
    order = await billing.create("acme", ChargeKind.APPOINTMENT, "visit-a", p.id)
    body = {
        "issuer_id": issuer["id"],
        "request_id": "synthetic-request",
        "order_id": order["invoice_id"],
        "net_cents": 4999,
        "tax_cents": 0,
        "tax_basis_reference": "synthetic arithmetic; no real VAT decision",
        "scenario": scenario,
    }
    return service, p, body


async def test_trial_is_nonfiscal_tenant_scoped_and_atomic_sequence(billing: BillingStore) -> None:
    service, p, body = await setup(billing)
    first, duplicate = await asyncio.gather(
        service.create(p, TrialRequest.model_validate(body)),
        service.create(p, TrialRequest.model_validate(body)),
    )
    assert first["id"] == duplicate["id"] and first["sequence"] == 1
    detail = await service.get(p, first["id"])
    assert detail["outcome"]["sri_authorised"] is False
    assert detail["outcome"]["signed"] is False
    assert "urn:tatos:non-fiscal-trial:1" in detail["xml"] and "NO ES FACTURA" in detail["xml"]
    assert len((await service.profiles("acme"))["issuers"]) == 1
    assert (await service.profiles("acme"))["issuers"][0]["next_sequence"] == 2
    with pytest.raises(TaxTrialError):
        await service.create(
            p, TrialRequest.model_validate({**body, "request_id": "another-request"})
        )
    foreign = Principal("globex", "owner", "staff", frozenset({"owner"}))
    assert await service.listing("globex") == []
    with pytest.raises(KeyError):
        await service.get(foreign, first["id"])
    with pytest.raises(KeyError):
        await service.create(foreign, TrialRequest.model_validate(body))
    assert len(await service.export_subject("acme", "patient")) == 1
    assert await service.erase_subject("globex", "patient") == 0
    assert await service.erase_subject("acme", "patient") == 1


async def test_trial_retry_never_allocates_a_second_sequence_or_claims_authorisation(
    billing: BillingStore,
) -> None:
    service, p, body = await setup(billing, "transient")
    first = await service.create(p, TrialRequest.model_validate(body))
    assert first["state"] == "retry_wait"
    with pytest.raises(TaxTrialError):
        await service.retry(p, first["id"], first["revision"])
    async with billing.db.engine.begin() as conn:
        await conn.execute(
            update(documents)
            .where(documents.c.tenant == "acme", documents.c.id == first["id"])
            .values(retry_at=utcnow() - timedelta(seconds=1))
        )
    retried = await service.retry(p, first["id"], first["revision"])
    assert retried["state"] == "simulated_accepted" and retried["sequence"] == first["sequence"]
    assert retried["outcome"]["sri_authorised"] is False and retried["attempts"] == 2
    with pytest.raises(TaxTrialError):
        await service.retry(p, first["id"], retried["revision"])


async def test_tax_trial_rejects_bad_totals_stale_profiles_and_production(
    billing: BillingStore,
) -> None:
    service, p, body = await setup(billing, "reject")
    with pytest.raises(TaxTrialError):
        await service.create(p, TrialRequest.model_validate({**body, "tax_cents": 1}))
    issuer = (await service.profiles("acme"))["issuers"][0]
    with pytest.raises(TaxTrialError):
        await service.configure(p, IssuerProfile.model_validate(PROFILE), issuer["id"], 0)
    trial = await service.create(p, TrialRequest.model_validate(body))
    assert trial["state"] == "simulated_rejected" and trial["outcome"]["sri_authorised"] is False
    blocked = TaxTrials(billing.db, billing.audit, "prod")
    with pytest.raises(TaxTrialError):
        await blocked.create(p, TrialRequest.model_validate(body))
