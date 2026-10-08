"""Re-identification tests (audit 2026-10-08, item 6). An attacker holds a full copy of the
database (a leaked backup) but not the secrets in the Kubernetes Secret. After a patient's
erasure, can they find out who the patient was, or link what remains back to them?

What is promised (ADR 0012, retention rule of 2026-09-30):
- contact data (phone, e-mail, Telegram id) leaves every table;
- the marketing history stays only as rows with a random id, no timestamps, unlinkable
  across campaigns: anonymous, not pseudonymous;
- an opted-out phone number is stored as a KEYED pseudonym that cannot be reversed by
  hashing every possible number;
- the clinical record (name included) is retained, restricted, under legal retention;
  it is the only place the name may stay, and the audit holds no contact data.
Synthetic data only."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from collections.abc import Iterator
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select

from orchestrator.api.app import create_app
from orchestrator.campaigns import ANON_PREFIX
from orchestrator.catalog import Catalog
from orchestrator.config import Settings
from orchestrator.db import metadata, utcnow
from orchestrator.inbound import address_key, channel_optouts
from orchestrator.llm import FakeLLM
from orchestrator.service import Orchestrator
from tests.test_inbound import SECRET, connect, payload, post

ADMIN = {"X-API-Key": "test-key"}
GOOD = "Hola {first_name}, te esperamos para tu control. Responde STOP para salir."
NAME = "Rosalinda Quishpe Andrango"
PHONE = "+593 99 123 4567"
EMAIL = "rosalinda.q@example.test"
TELEGRAM = "778899001"
# Tables where the (restricted) clinical record keeps the patient's name by law.
CLINICAL = {"crm_patients", "crm_clinical_notes", "clinical_documents"}


@pytest.fixture
def world(settings: Settings, catalog: Catalog) -> Iterator[tuple[TestClient, Orchestrator]]:
    settings.tenant_packs = {"acme": "dental"}
    settings.campaign_default_holdout_pct = 0
    settings.meta_app_secret = SecretStr(SECRET)
    settings.whatsapp_verify_token = SecretStr("verify")
    settings.pseudonym_key = SecretStr("k" * 48)
    orch = Orchestrator(settings, catalog=catalog, llm=FakeLLM())
    with TestClient(create_app(settings, orch)) as c:
        yield c, orch


async def dump(orch: Orchestrator) -> dict[str, str]:
    """Every table, every row, as text: what a leaked backup shows."""
    out: dict[str, str] = {}
    async with orch.db.engine.connect() as conn:
        for name, table in metadata.tables.items():
            rows = (await conn.execute(select(table))).mappings().all()
            out[name] = json.dumps([dict(r) for r in rows], default=str, ensure_ascii=False)
    return out


def seed_patient(c: TestClient) -> None:
    patient = {
        "id": "p-77",
        "display_name": NAME,
        "phone": PHONE,
        "email": EMAIL,
        "telegram_chat_id": TELEGRAM,
    }
    assert c.post("/v1/crm/patients", json=patient, headers=ADMIN).status_code == 201
    past = (utcnow() - timedelta(days=400)).isoformat()
    appt = c.post(
        "/v1/crm/appointments", json={"patient_id": "p-77", "starts_at": past}, headers=ADMIN
    ).json()
    c.post(f"/v1/crm/appointments/{appt['id']}/status", json={"status": "completed"}, headers=ADMIN)
    for purpose in ("marketing", "analytics"):
        c.put(
            f"/v1/subjects/p-77/consents/{purpose}",
            json={"granted": True, "source": "form"},
            headers=ADMIN,
        )


def run_campaign(c: TestClient, name: str) -> str:
    created = c.post(
        "/v1/campaigns",
        json={
            "name": name,
            "kind": "reactivation",
            "segment": "dormant",
            "template": GOOD,
            "mode": "simulation",
        },
        headers=ADMIN,
    ).json()
    c.post(f"/v1/campaigns/{created['id']}/approve", json={}, headers=ADMIN)
    c.post(f"/v1/campaigns/{created['id']}/send", headers=ADMIN)
    return str(created["id"])


async def test_after_erasure_a_leaked_database_names_nobody(
    world: tuple[TestClient, Orchestrator],
) -> None:
    c, orch = world
    seed_patient(c)
    first, second = run_campaign(c, "Uno"), run_campaign(c, "Dos")
    before = await dump(orch)
    assert PHONE.replace(" ", "") in before["crm_patients"].replace(" ", "")  # the probe works

    erased = c.delete("/v1/subjects/p-77", headers=ADMIN)
    assert erased.status_code == 200
    after = await dump(orch)

    contact = [PHONE, PHONE.replace(" ", ""), "991234567", EMAIL, TELEGRAM]
    for table, text in after.items():
        for value in contact:
            assert value not in text, f"{value!r} survived erasure in {table}"
        if table not in CLINICAL:
            assert NAME not in text, f"the name survived erasure in {table}"
            assert "Rosalinda" not in text, table

    # The marketing history: one row per campaign, random ids, no timestamps, no link.
    rows = json.loads(after["campaign_recipients"])
    assert {r["campaign_id"] for r in rows} == {first, second}
    ids = [r["patient_id"] for r in rows]
    assert all(i.startswith(ANON_PREFIX) for i in ids)
    assert len(set(ids)) == len(ids)  # two campaigns cannot be joined on the id
    for r in rows:
        assert r["sent_at"] is None and r["seen_at"] is None and r["claimed_at"] is None
        suffix = r["patient_id"][len(ANON_PREFIX) :]
        # Not a hash of the old id (that could be recomputed by anyone who knows it).
        for derived in ("p-77", "acme:p-77", f"{r['campaign_id']}:p-77"):
            assert suffix not in {
                hashlib.sha256(derived.encode()).hexdigest(),
                hashlib.md5(derived.encode()).hexdigest(),
            }

    # The audit proves the erasure without holding the contact data it erased.
    assert "subject.erased" in after["audit_events"]
    # And the subject's own export is now empty of contact data.
    export = c.get("/v1/subjects/p-77/export", headers=ADMIN).text
    for value in contact:
        assert value not in export


def test_opt_out_pseudonyms_cannot_be_reversed_without_the_key(
    world: tuple[TestClient, Orchestrator],
) -> None:
    c, orch = world
    connect(c)
    sender = "15550001111"  # tests.test_inbound.SENDER
    assert post(c, payload("STOP", "wamid.stop-1")) == 200

    async def stored() -> list[str]:
        async with orch.db.engine.connect() as conn:
            return list((await conn.execute(select(channel_optouts.c.address_key))).scalars())

    [key] = asyncio.run(stored())
    assert sender not in key
    # The attacker knows the algorithm and enumerates every number, but has no key: the
    # plain hashes they can compute never match what is stored.
    attacker = {
        hashlib.sha256(f"acme:whatsapp:{sender}".encode()).hexdigest(),
        hashlib.sha256(sender.encode()).hexdigest(),
        hmac.new(b"", f"acme:whatsapp:{sender}".encode(), hashlib.sha256).hexdigest(),
    }
    assert key not in attacker
    # The service, holding the key, still recognises the number (STOP keeps working).
    assert key == address_key(b"k" * 48, "acme", "whatsapp", sender)
    assert asyncio.run(orch.inbound.is_opted_out("acme", "whatsapp", f"+{sender}"))
    # A different deployment key gives unrelated pseudonyms: no cross-deployment linkage.
    assert address_key(b"x" * 48, "acme", "whatsapp", sender) != key


def test_prod_refuses_to_start_without_a_pseudonym_key(settings: Settings) -> None:
    settings.app_env = "prod"
    settings.pseudonym_key = None
    with pytest.raises(ValueError, match="PSEUDONYM_KEY"):
        settings.pseudonym_secret()
