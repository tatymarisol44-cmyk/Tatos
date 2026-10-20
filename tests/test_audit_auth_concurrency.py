"""Concurrent sign-in failures must not erase one another. Synthetic accounts only."""

from __future__ import annotations

import asyncio
import os
import threading
import uuid
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from pydantic import SecretStr
from sqlalchemy import update

from orchestrator import staff_login
from orchestrator.auth import Role
from orchestrator.catalog import Catalog
from orchestrator.config import Settings
from orchestrator.db import aware, utcnow
from orchestrator.llm import FakeLLM
from orchestrator.service import Orchestrator
from orchestrator.staff_login import SignInError, totp_at, unb32


@pytest.mark.parametrize("expired_lock", [False, True], ids=["fresh", "expired"])
@pytest.mark.parametrize(
    "backend",
    [
        "sqlite",
        pytest.param(
            "postgres",
            marks=pytest.mark.skipif(
                not os.environ.get("TEST_POSTGRES_URL"), reason="set TEST_POSTGRES_URL to run"
            ),
        ),
    ],
)
@pytest.mark.parametrize("attempts", [2, 5, 7])
async def test_concurrent_wrong_passwords_keep_the_account_lock_budget(
    settings: Settings,
    catalog: Catalog,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    attempts: int,
    backend: str,
    expired_lock: bool,
) -> None:
    settings.database_url = SecretStr(
        "sqlite+aiosqlite:///" + (tmp_path / "login.sqlite").as_posix()
    )
    if backend == "postgres":
        settings.database_url = SecretStr(
            os.environ["TEST_POSTGRES_URL"].replace("postgresql://", "postgresql+psycopg://", 1)
        )
        settings.db_auto_migrate = True
    orch = Orchestrator(settings, catalog=catalog, llm=FakeLLM())
    await orch.start()
    try:
        login = orch.staff_logins
        email = f"synthetic-{uuid.uuid4().hex}@example.test"
        member = await login.add_member(
            "acme", "service", "Synthetic staff", email, [Role.RECEPTION]
        )
        token = urlsplit(member["link"]).fragment.removeprefix("clave=")
        enrollment = await login.use_link(token, "Cuaderno-Azul-2026")
        secret = unb32(enrollment["secret"])
        await login.enroll(
            enrollment["challenge"], totp_at(secret, staff_login.current_step()), False
        )
        if expired_lock:
            async with orch.db.engine.begin() as connection:
                await connection.execute(
                    update(staff_login.staff_logins)
                    .where(staff_login.staff_logins.c.principal_id == member["id"])
                    .values(locked_until=utcnow() - timedelta(minutes=1))
                )
        barrier = threading.Barrier(attempts)
        original = staff_login.check_password

        def simultaneous_password_check(password: str, stored: str | None) -> bool:
            # Every request has read the account before any is allowed to write.
            barrier.wait(timeout=20)
            return original(password, stored)

        monkeypatch.setattr(staff_login, "check_password", simultaneous_password_check)
        results = await asyncio.gather(
            *(login.begin(email, "synthetic-wrong-password") for _ in range(attempts)),
            return_exceptions=True,
        )
        assert all(isinstance(result, SignInError) for result in results)
        row = await login._row("acme", member["id"])
        assert row is not None
        expected = 0 if attempts >= staff_login.LOCK_AFTER else attempts
        assert row["failed_attempts"] == expected
        locked = aware(row["locked_until"])
        if attempts >= staff_login.LOCK_AFTER:
            assert locked is not None and locked > utcnow()
        else:
            assert locked is None
    finally:
        await orch.close()
