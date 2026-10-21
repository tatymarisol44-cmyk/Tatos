"""Patient activation/recovery serialize across separate PostgreSQL pools."""

import asyncio
import secrets
from typing import Any

import pytest

from orchestrator.patient_login import PatientLoginError
from tests.test_postgres_concurrency import POSTGRES_URL
from tests.test_postgres_concurrency import replicas as replicas

pytestmark = pytest.mark.skipif(POSTGRES_URL is None, reason="set TEST_POSTGRES_URL to run")


async def test_two_activations_have_one_winner(replicas: tuple[Any, Any, str]) -> None:
    a, b, tenant = replicas
    await a.crm.create_patient(tenant, {"display_name": "Synthetic"}, "test", patient_id="one")
    _, first = await a.principals.create_patient_access(tenant, "one", "test", 3)
    _, second = await a.principals.create_patient_access(tenant, "one", "test", 3)
    p1, p2 = await a.principals.resolve(first), await b.principals.resolve(second)
    result = await asyncio.gather(
        a.patient_logins.activate(p1, first, f"{tenant}-a@example.test", secrets.token_urlsafe(24)),
        b.patient_logins.activate(
            p2, second, f"{tenant}-b@example.test", secrets.token_urlsafe(24)
        ),
        return_exceptions=True,
    )
    assert sum(isinstance(value, dict) for value in result) == 1
    assert sum(isinstance(value, PatientLoginError) for value in result) == 1


async def test_recovery_racing_old_password_leaves_no_old_session(
    replicas: tuple[Any, Any, str],
) -> None:
    a, b, tenant = replicas
    await a.crm.create_patient(tenant, {"display_name": "Synthetic"}, "test", patient_id="one")
    _, key = await a.principals.create_patient_access(tenant, "one", "test", 3)
    principal = await a.principals.resolve(key)
    email, old = f"{tenant}@example.test", secrets.token_urlsafe(24)
    original = await a.patient_logins.activate(principal, key, email, old)
    recovery = await a.patient_logins.recovery(tenant, "one", "test")
    principal = await b.principals.resolve(recovery["key"])
    result = await asyncio.gather(
        a.patient_logins.sign_in(email, old),
        b.patient_logins.activate(principal, recovery["key"], email, secrets.token_urlsafe(24)),
        return_exceptions=True,
    )
    assert isinstance(result[1], dict), result
    assert await a.patient_logins.resolve(original["key"]) is None
    if isinstance(result[0], dict):
        assert await a.patient_logins.resolve(result[0]["key"]) is None
    else:
        assert isinstance(result[0], PatientLoginError)
    assert await a.patient_logins.resolve(result[1]["key"]) is not None
