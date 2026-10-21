"""Booking options remain exclusive across actual PostgreSQL connections."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from orchestrator.agenda import DAYS, Block
from orchestrator.crm import CrmError
from tests.test_postgres_concurrency import POSTGRES_URL
from tests.test_postgres_concurrency import replicas as replicas

pytestmark = pytest.mark.skipif(POSTGRES_URL is None, reason="set TEST_POSTGRES_URL to run")


async def test_only_one_replica_can_book_the_same_offered_time(
    replicas: tuple[Any, Any, str],
) -> None:
    a, b, tenant = replicas
    await a.professionals.add(
        tenant,
        "test",
        professional_id="booking-doc",
        display_name="Synthetic Professional",
        pack_id="dental",
    )
    await a.agenda.set_hours(
        tenant, "booking-doc", [Block.parse(d, "08:00-20:00", 50) for d in DAYS], "test"
    )
    for pid in ("one", "two"):
        await a.crm.create_patient(
            tenant, {"display_name": f"Synthetic {pid}"}, "test", patient_id=pid
        )
    options = await a.agenda.free_slots(tenant, "booking-doc", days=2)
    from datetime import datetime

    start = datetime.fromisoformat(options[0]["starts_at"])

    async def book(pod: Any, pid: str) -> Any:
        return await pod.crm.create_appointment(
            tenant,
            pid,
            starts_at=start,
            duration_min=50,
            kind="sesion",
            price=0,
            actor="test",
            professional_id="booking-doc",
            require_available_slot=True,
        )

    results = await asyncio.gather(book(a, "one"), book(b, "two"), return_exceptions=True)
    assert sum(isinstance(r, dict) for r in results) == 1
    assert sum(isinstance(r, CrmError) for r in results) == 1
    assert len(await a.crm.list_appointments(tenant)) == 1
