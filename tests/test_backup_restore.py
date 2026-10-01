"""Backup and restore drill on a real Postgres (second audit): what deploy/backup/*.sh do,
step by step, with the same pg_dump/pg_restore flags. Needs TEST_POSTGRES_URL and the
Postgres client tools (PATH, or PG_BIN), matching the server's major version."""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import uuid
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import pytest
from pydantic import SecretStr
from sqlalchemy import func, select

from orchestrator import migrate
from orchestrator.catalog import Catalog
from orchestrator.config import Settings
from orchestrator.db import metadata, utcnow
from orchestrator.governance import Purpose
from orchestrator.llm import FakeLLM
from orchestrator.service import Orchestrator

POSTGRES_URL = os.environ.get("TEST_POSTGRES_URL")


def _tool(name: str) -> str | None:
    bin_dir = os.environ.get("PG_BIN")
    if bin_dir:
        for candidate in (Path(bin_dir) / name, Path(bin_dir) / f"{name}.exe"):
            if candidate.exists():
                return str(candidate)
    return shutil.which(name)


pytestmark = pytest.mark.skipif(
    POSTGRES_URL is None or _tool("pg_dump") is None,
    reason="set TEST_POSTGRES_URL and put pg_dump/pg_restore on PATH (or PG_BIN)",
)


@pytest.fixture
def databases() -> Iterator[tuple[str, str]]:
    """A source and an empty restore target, dropped afterwards."""
    import psycopg

    assert POSTGRES_URL is not None
    base = POSTGRES_URL.rsplit("/", 1)[0]
    names = [f"bk_{uuid.uuid4().hex[:8]}", f"rs_{uuid.uuid4().hex[:8]}"]
    with psycopg.connect(base + "/postgres", autocommit=True) as conn:
        for name in names:
            conn.execute(f"CREATE DATABASE {name}")
    yield base + "/" + names[0], base + "/" + names[1]
    with psycopg.connect(base + "/postgres", autocommit=True) as conn:
        for name in names:
            conn.execute(f"DROP DATABASE {name} WITH (FORCE)")


def _sqlalchemy_url(url: str) -> SecretStr:
    return SecretStr(url.replace("postgresql://", "postgresql+psycopg://", 1))


async def _counts(orch: Orchestrator) -> dict[str, int]:
    async with orch.db.engine.connect() as conn:
        return {
            name: int((await conn.execute(select(func.count()).select_from(table))).scalar_one())
            for name, table in sorted(metadata.tables.items())
        }


def _dump_and_restore(source: str, target: str, dump: Path) -> None:
    pg_dump, pg_restore = _tool("pg_dump"), _tool("pg_restore")
    assert pg_dump and pg_restore
    flags = ["--no-owner", "--no-privileges"]
    subprocess.run([pg_dump, "--format=custom", *flags, f"--file={dump}", source], check=True)
    subprocess.run(
        [pg_restore, "--exit-on-error", *flags, f"--dbname={target}", str(dump)], check=True
    )


async def test_backup_restores_whole_and_verified(
    settings: Settings, catalog: Catalog, databases: tuple[str, str], tmp_path: Path
) -> None:
    source, target = databases
    settings.tenant_packs = {"acme": "dental"}
    settings.campaign_default_holdout_pct = 0
    settings.db_auto_migrate = True
    settings.database_url = _sqlalchemy_url(source)
    orch = Orchestrator(settings, catalog=catalog, llm=FakeLLM())
    await orch.start()
    try:  # a clinic's worth of records, most tables touched
        for i in range(5):
            pid = f"p{i}"
            await orch.crm.create_patient("acme", {"display_name": f"Ana {i}"}, "r", patient_id=pid)
            for purpose in (Purpose.MARKETING, Purpose.ANALYTICS):
                await orch.consents.record("acme", pid, purpose, True, source="f", actor="r")
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
        await orch.principals.create_patient_access("acme", "p0", "r", 30)
        await orch.campaigns.create(
            "acme",
            name="Vuelve",
            kind="reactivation",
            segment="dormant",
            channel="telegram",
            template="Hola {first_name}. Responde STOP para salir.",
            actor="r",
            mode="simulation",
        )
        await orch.knowledge.add("acme", "Políticas", "Cancelaciones con 24 horas de aviso.")
        anchors = await orch.audit.anchors()  # taken with the backup (backup.sh)
        before = await _counts(orch)
    finally:
        await orch.close()

    await asyncio.to_thread(_dump_and_restore, source, target, tmp_path / "agency.dump")

    settings.db_auto_migrate = False  # a restore must already be at head (restore.sh)
    settings.database_url = _sqlalchemy_url(target)
    restored = Orchestrator(settings, catalog=catalog, llm=FakeLLM())
    await restored.start()
    try:
        assert await migrate.current(restored.db.engine) == migrate.head()
        assert await _counts(restored) == before and before["audit_events"] > 10
        verdict = await restored.audit.verify("acme", anchors)
        assert verdict["ok"] is True and verdict["anchors_checked"] == 1
        patient = await restored.crm.get_patient("acme", "p3", actor=None)
        assert patient["display_name"] == "Ana 3"
        [doc] = await restored.knowledge.documents("acme")
        assert doc.title == "Políticas"
    finally:
        await restored.close()
