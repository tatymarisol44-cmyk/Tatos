"""Loyalty campaigns: compliance, approval, consent/channel/cap filters, holdout, delivery,
lift measurement and data-subject rights."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import select, update

from orchestrator.campaigns import (
    CampaignError,
    arm_for,
    campaigns,
    contact_budget,
    placeholders_ok,
    recipients,
    render,
)
from orchestrator.catalog import Catalog
from orchestrator.config import Settings
from orchestrator.db import utcnow
from orchestrator.governance import Purpose
from orchestrator.llm import FakeLLM
from orchestrator.service import Orchestrator

GOOD = "Hola {first_name}, ya toca tu control. Agenda respondiendo aquí. Responde STOP para salir."


def _telegram_ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"ok": True})


@pytest.fixture
async def clinic(settings: Settings, catalog: Catalog) -> Any:
    settings.tenant_packs = {"acme": "dental"}
    settings.campaign_default_holdout_pct = 0  # deterministic tests: everyone is treated
    settings.telegram_bot_token = SecretStr("123:test-token")  # a stand-in bot that says ok
    orch = Orchestrator(settings, catalog=catalog, llm=FakeLLM())
    await orch.start()
    orch.campaigns.telegram._http = httpx.AsyncClient(transport=httpx.MockTransport(_telegram_ok))
    yield orch
    await orch.close()


async def _dormant(
    orch: Orchestrator, pid: str, *, consent: bool = True, chat: str | None = "1"
) -> None:
    """A patient whose last visit was a year ago (dormant with 6-month recalls)."""
    await orch.crm.create_patient(
        "acme", {"display_name": f"Ana {pid}", "telegram_chat_id": chat}, "r", patient_id=pid
    )
    appt = await orch.crm.create_appointment(
        "acme",
        pid,
        starts_at=utcnow() - timedelta(days=400),
        duration_min=30,
        kind="checkup",
        price=50,
        actor="r",
    )
    await orch.crm.set_appointment_status("acme", appt["id"], "completed", "r")
    await orch.consents.record("acme", pid, Purpose.ANALYTICS, True, source="form", actor="r")
    if consent:
        await orch.consents.record("acme", pid, Purpose.MARKETING, True, source="form", actor="r")


# --- pure helpers ---------------------------------------------------------------------------


def test_holdout_split_is_deterministic_and_proportional() -> None:
    assert arm_for("c1", "p1", 20) == arm_for("c1", "p1", 20)
    control = sum(arm_for("c1", f"p{i}", 20) == "control" for i in range(2000))
    assert 320 < control < 480  # ~20%
    assert {arm_for("c1", f"p{i}", 0) for i in range(50)} == {"treatment"}


def test_template_helpers() -> None:
    assert placeholders_ok("Hi {first_name} {last_name} {phone}") == ["last_name", "phone"]
    assert render("Hola {first_name}!", "Ana María López") == "Hola Ana!"
    assert render("Hola {first_name}!", "") == "Hola !"


# --- lifecycle ---------------------------------------------------------------------------


async def test_compliant_copy_waits_for_approval(clinic: Orchestrator) -> None:
    await _dormant(clinic, "p1")
    campaign = await clinic.campaigns.create(
        "acme",
        name="Vuelve",
        kind="reactivation",
        segment="dormant",
        channel="telegram",
        template=GOOD,
        actor="ana",
    )
    assert campaign["status"] == "pending_approval"
    assert campaign["compliance"]["ok"] is True
    assert campaign["recipients"] == {"treatment:pending": 1}
    with pytest.raises(CampaignError, match="only approved"):
        await clinic.campaigns.send("acme", campaign["id"], "ana")


async def test_non_compliant_copy_stays_in_draft_until_fixed(clinic: Orchestrator) -> None:
    await _dormant(clinic, "p1")
    campaign = await clinic.campaigns.create(
        "acme",
        name="X",
        kind="recall",
        segment="dormant",
        channel="telegram",
        template="Tu tratamiento de conducto con resultados garantizados, {last_name}",
        actor="ana",
    )
    assert campaign["status"] == "draft"
    violations = campaign["compliance"]["violations"]
    assert "banned_claim:garantizado" in violations  # substring match covers "garantizados"
    assert any(v.startswith("clinical_detail:") for v in violations)
    assert "placeholder:last_name" in violations and "missing_opt_out:reply STOP" in violations
    with pytest.raises(CampaignError, match="not pending approval"):
        await clinic.campaigns.approve("acme", campaign["id"], "owner")
    fixed = await clinic.campaigns.update_template("acme", campaign["id"], GOOD, "ana")
    assert fixed["status"] == "pending_approval"


async def test_copywriter_draft_always_has_an_opt_out(clinic: Orchestrator) -> None:
    await _dormant(clinic, "p1")
    campaign = await clinic.campaigns.create(
        "acme", name="auto", kind="recall", segment="dormant", channel="telegram", actor="ana"
    )
    assert "{first_name}" in campaign["template"] and "STOP" in campaign["template"]
    assert campaign["status"] == "pending_approval"


async def test_big_discounts_need_the_owner(clinic: Orchestrator) -> None:
    await _dormant(clinic, "p1")
    campaign = await clinic.campaigns.create(
        "acme",
        name="promo",
        kind="reactivation",
        segment="dormant",
        channel="telegram",
        template="Hola {first_name}: 40% en tu limpieza este mes. Responde STOP para salir.",
        actor="ana",
    )
    with pytest.raises(CampaignError, match="owner must approve"):
        await clinic.campaigns.approve("acme", campaign["id"], "ana")
    approved = await clinic.campaigns.approve("acme", campaign["id"], "owner", owner_approval=True)
    assert approved["status"] == "approved" and approved["approved_by"] == "owner"


async def test_eligibility_is_decided_before_the_split(
    clinic: Orchestrator, settings: Settings
) -> None:
    await _dormant(clinic, "yes")
    await _dormant(clinic, "no-consent", consent=False)
    await _dormant(clinic, "no-chat", chat=None)
    settings.campaign_default_holdout_pct = 50
    campaign = await clinic.campaigns.create(
        "acme",
        name="c",
        kind="reactivation",
        segment="dormant",
        channel="telegram",
        template=GOOD,
        actor="ana",
    )
    # Ineligible patients are in neither arm: they are reported, not assigned.
    assert campaign["population"] == {
        "segment": 3,
        "eligible": 1,
        "excluded": {"no_marketing_consent": 1, "no_channel": 1},
    }
    await clinic.campaigns.approve("acme", campaign["id"], "owner")
    sent = await clinic.campaigns.send("acme", campaign["id"], "ana")
    [row] = await _rows(clinic, campaign["id"])
    assert row["patient_id"] == "yes"
    assert row["status"] == ("held_out" if row["arm"] == "control" else "sent")
    assert sent["status"] == "completed"
    with pytest.raises(CampaignError):
        await clinic.campaigns.send("acme", campaign["id"], "ana")  # never twice


async def _rows(orch: Orchestrator, campaign_id: str) -> list[dict[str, Any]]:
    from sqlalchemy import select

    async with orch.db.engine.connect() as conn:
        rows = (
            (await conn.execute(select(recipients).where(recipients.c.campaign_id == campaign_id)))
            .mappings()
            .all()
        )
    return [dict(r) for r in rows]


async def test_withdrawn_consent_is_checked_at_send_time(clinic: Orchestrator) -> None:
    await _dormant(clinic, "p1")
    campaign = await clinic.campaigns.create(
        "acme",
        name="c",
        kind="recall",
        segment="dormant",
        channel="telegram",
        template=GOOD,
        actor="ana",
    )
    await clinic.campaigns.approve("acme", campaign["id"], "owner")
    await clinic.consents.record("acme", "p1", Purpose.MARKETING, False, source="STOP", actor="bot")
    sent = await clinic.campaigns.send("acme", campaign["id"], "ana")
    assert sent["outcomes"] == {"skipped_no_consent": 1}


async def test_monthly_frequency_cap(clinic: Orchestrator) -> None:
    await _dormant(clinic, "p1")  # dental pack: at most 2 messages a month
    outcomes = []
    for i, mode in enumerate(["simulation", "simulation", "live", "live", "live"]):
        c = await clinic.campaigns.create(
            "acme",
            name=f"c{i}",
            kind="education",
            segment="dormant",
            channel="telegram",
            template=GOOD,
            actor="ana",
            mode=mode,
        )
        await clinic.campaigns.approve("acme", c["id"], "owner")
        outcomes.append((await clinic.campaigns.send("acme", c["id"], "ana"))["outcomes"])
    # Rehearsals take no room under the cap; real messages do.
    assert outcomes == [
        {"dry_run": 1},
        {"dry_run": 1},
        {"sent": 1},
        {"sent": 1},
        {"skipped_cap": 1},
    ]
    async with clinic.db.engine.connect() as conn:
        assert (await conn.execute(select(contact_budget.c.used))).scalar_one() == 2


async def test_live_telegram_delivery(clinic: Orchestrator, settings: Settings) -> None:
    await _dormant(clinic, "ok", chat="100")
    await _dormant(clinic, "fails", chat="200")
    sent_bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        sent_bodies.append(body)
        if body["chat_id"] == "200":
            return httpx.Response(400, json={"ok": False, "description": "chat not found"})
        return httpx.Response(200, json={"ok": True})

    settings.telegram_bot_token = SecretStr("123:secret")
    clinic.campaigns.telegram._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    c = await clinic.campaigns.create(
        "acme",
        name="c",
        kind="recall",
        segment="dormant",
        channel="telegram",
        template=GOOD,
        actor="ana",
    )
    await clinic.campaigns.approve("acme", c["id"], "owner")
    sent = await clinic.campaigns.send("acme", c["id"], "ana")
    assert sent["outcomes"] == {"sent": 1, "failed": 1}
    texts = {b["chat_id"]: b["text"] for b in sent_bodies}
    assert texts["100"].startswith("Hola Ana,")  # personalised with the first name only
    rows = {r["patient_id"]: r for r in await _rows(clinic, c["id"])}
    assert rows["fails"]["error"] == "RuntimeError"
    audit = await clinic.audit.list("acme")
    assert "123:secret" not in json.dumps([e.to_dict() for e in audit])  # token never logged


async def test_results_measure_lift_against_the_holdout(
    clinic: Orchestrator, settings: Settings
) -> None:
    for i in range(12):
        await _dormant(clinic, f"p{i}")
    settings.campaign_default_holdout_pct = 50
    c = await clinic.campaigns.create(
        "acme",
        name="c",
        kind="reactivation",
        segment="dormant",
        channel="telegram",
        template=GOOD,
        actor="ana",
    )
    await clinic.campaigns.approve("acme", c["id"], "owner")
    sent = await clinic.campaigns.send("acme", c["id"], "ana")
    rows = await _rows(clinic, c["id"])
    treated = [r["patient_id"] for r in rows if r["arm"] == "treatment"]
    control = [r["patient_id"] for r in rows if r["arm"] == "control"]
    assert treated and control
    # Every treated patient books after the message; nobody in the control group does.
    for pid in treated:
        await clinic.crm.create_appointment(
            "acme",
            pid,
            starts_at=utcnow() + timedelta(days=5),
            duration_min=30,
            kind="checkup",
            price=50,
            actor="bot",
        )
    early = await clinic.campaigns.results("acme", c["id"])
    assert early["status"] == "provisional" and "lift_abs" not in early["itt"]
    later = utcnow() + timedelta(days=31)
    results = await clinic.campaigns.results("acme", c["id"], now=later)
    assert results["status"] == "final"
    treatment = results["itt"]["arms"]["treatment"]
    assert {k: treatment[k] for k in ("n", "converted", "rate")} == {
        "n": len(treated),
        "converted": len(treated),
        "rate": 1.0,
    }
    assert results["itt"]["arms"]["control"]["converted"] == 0
    assert results["itt"]["lift_abs"] == 1.0 and results["itt"]["lift_rel"] is None
    assert results["itt"]["test"] == "fisher_exact_two_sided"
    assert results["conclusion"] == "inconclusive (arms too small)"
    assert (
        results["window_ends_at"]
        == (datetime.fromisoformat(sent["sent_at"]) + timedelta(days=30)).isoformat()
    )


async def test_bookings_outside_the_window_do_not_count(clinic: Orchestrator) -> None:
    await _dormant(clinic, "p1")
    c = await clinic.campaigns.create(
        "acme",
        name="c",
        kind="recall",
        segment="dormant",
        channel="telegram",
        template=GOOD,
        actor="ana",
    )
    await clinic.campaigns.approve("acme", c["id"], "owner")
    await clinic.campaigns.send("acme", c["id"], "ana")
    # Pretend the campaign went out two months ago; a booking today is outside 30 days.
    async with clinic.db.engine.begin() as conn:
        await conn.execute(update(campaigns).values(sent_at=utcnow() - timedelta(days=60)))
    await clinic.crm.create_appointment(
        "acme",
        "p1",
        starts_at=utcnow() + timedelta(days=1),
        duration_min=30,
        kind="x",
        price=0,
        actor="r",
    )
    results = await clinic.campaigns.results("acme", c["id"])
    assert results["status"] == "final"
    assert results["itt"]["arms"]["treatment"]["converted"] == 0


async def test_cancel_and_validation(clinic: Orchestrator) -> None:
    await _dormant(clinic, "p1")
    c = await clinic.campaigns.create(
        "acme",
        name="c",
        kind="recall",
        segment="dormant",
        channel="telegram",
        template=GOOD,
        actor="ana",
    )
    assert (await clinic.campaigns.cancel("acme", c["id"], "ana"))["status"] == "cancelled"
    with pytest.raises(CampaignError, match="only for sent"):
        await clinic.campaigns.results("acme", c["id"])
    with pytest.raises(CampaignError, match="unknown kind"):
        await clinic.campaigns.create(
            "acme", name="c", kind="spam", segment="dormant", channel="telegram", actor="a"
        )
    with pytest.raises(CampaignError, match="unsupported channel"):
        await clinic.campaigns.create(
            "acme", name="c", kind="recall", segment="dormant", channel="sms", actor="a"
        )
    with pytest.raises(CampaignError, match="unknown mode"):
        await clinic.campaigns.create(
            "acme",
            name="c",
            kind="recall",
            segment="dormant",
            channel="telegram",
            actor="a",
            mode="pretend",
        )
    with pytest.raises(KeyError):
        await clinic.campaigns.get("globex", c["id"])
    assert [x["id"] for x in await clinic.campaigns.list_all("acme")] == [c["id"]]


async def test_subject_rights_cover_campaign_history(clinic: Orchestrator) -> None:
    await _dormant(clinic, "p1")
    c = await clinic.campaigns.create(
        "acme",
        name="c",
        kind="recall",
        segment="dormant",
        channel="telegram",
        template=GOOD,
        actor="ana",
    )
    await clinic.campaigns.approve("acme", c["id"], "owner")
    await clinic.campaigns.send("acme", c["id"], "ana")
    exported = await clinic.export_subject("acme", "p1", actor="dpo")
    [message] = exported["campaign_messages"]
    assert message["status"] == "sent" and message["sent_at"]
    assert exported["crm"]["patient"]["id"] == "p1"
    erased = await clinic.erase_subject("acme", "p1", actor="dpo")
    assert erased["campaign_messages_anonymised"] == 1
    assert await clinic.campaigns.export_subject("acme", "p1") == []
    # The outcome stays for the clinic's statistics, without identity or timestamps.
    counts = (await clinic.campaigns.get("acme", c["id"]))["recipients"]
    assert sum(counts.values()) == 1
    [row] = await _rows(clinic, c["id"])
    assert row["patient_id"].startswith("anon:") and row["sent_at"] is None
