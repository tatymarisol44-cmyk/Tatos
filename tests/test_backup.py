"""The encrypted backup (RF-PRV-04): a restore drill (create, restore into an empty
database, every row identical), and a backup that does not open with another key, when
altered, or when cut short. Synthetic data only."""

from __future__ import annotations

import io
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import MetaData, select

import orchestrator.backup as backup
import orchestrator.cli as cli
from orchestrator.api.app import create_app
from orchestrator.catalog import Catalog
from orchestrator.config import Settings
from orchestrator.db import Database
from orchestrator.llm import FakeLLM
from orchestrator.schema import prepare
from orchestrator.service import Orchestrator

ADMIN = {"X-API-Key": "test-key"}
NOTE = "Sesión 3: refiere mejor descanso."


def _sqlite(path: Path) -> SecretStr:
    return SecretStr(f"sqlite+aiosqlite:///{path.as_posix()}")


@pytest.fixture
def source(settings: Settings, catalog: Catalog, tmp_path: Path) -> Settings:
    """A database holding one of each kind of value: dates, decimals, bytes, JSON."""
    settings.database_url = _sqlite(tmp_path / "source.db")
    orch = Orchestrator(settings, catalog=catalog, llm=FakeLLM())
    with TestClient(create_app(settings, orch)) as c:
        body = {"id": "p-1", "display_name": "Paciente Demo", "birth_date": "1990-05-04"}
        assert c.post("/v1/crm/patients", json=body, headers=ADMIN).status_code == 201
        visit = {"patient_id": "p-1", "starts_at": "2026-10-01T15:00:00+00:00", "price": 35.5}
        assert c.post("/v1/crm/appointments", json=visit, headers=ADMIN).status_code == 201
        staff = c.post(
            "/v1/admin/staff", json={"name": "dra", "roles": ["reviewer"]}, headers=ADMIN
        ).json()
        dra = {"X-API-Key": staff["key"]}
        made = c.post(
            "/v1/clinical/instruments/from-template", json={"template": "gad7"}, headers=dra
        )
        assert made.status_code == 201
        upload = c.post(
            "/v1/clinical/patients/p-1/files",
            files={"file": ("informe.pdf", b"%PDF-1.7 \x00\xff synthetic", "application/pdf")},
            data={"label": "Informe"},
            headers=dra,
        )
        assert upload.status_code == 201, upload.text
    return settings


async def _everything(settings: Settings) -> dict[str, list[tuple[Any, ...]]]:
    db = Database(settings)
    try:
        async with db.engine.connect() as conn:
            reflected = MetaData()
            await conn.run_sync(lambda sync: reflected.reflect(bind=sync))
            return {
                t.name: sorted((tuple(r) for r in await conn.execute(select(t))), key=repr)
                for t in reflected.sorted_tables
            }
    finally:
        await db.close()


async def _backup(settings: Settings, public: str) -> bytes:
    db = Database(settings)
    try:
        out = io.BytesIO()
        await backup.create(db.engine, out, public)
        return out.getvalue()
    finally:
        await db.close()


async def _empty(settings: Settings, tmp_path: Path, name: str) -> Settings:
    target = settings.model_copy(update={"database_url": _sqlite(tmp_path / name)})
    db = Database(target)
    try:
        await prepare(db)
    finally:
        await db.close()
    return target


async def test_the_restore_drill(source: Settings, tmp_path: Path) -> None:
    public, private = backup.keygen()
    sealed = await _backup(source, public)
    assert b"Paciente Demo" not in sealed and b"%PDF" not in sealed

    summary = backup.check(io.BytesIO(sealed), private)
    assert summary.counts["crm_patients"] == 1 and summary.counts["audit_events"] > 0

    target = await _empty(source, tmp_path, "target.db")
    db = Database(target)
    try:
        restored = await backup.restore(db.engine, io.BytesIO(sealed), private)
        assert restored.counts["clinical_files"] == 1
        # Into a database that already holds data: refused, nothing changed.
        with pytest.raises(backup.BackupError, match="empty database"):
            await backup.restore(db.engine, io.BytesIO(sealed), private)
    finally:
        await db.close()
    before, after = await _everything(source), await _everything(target)
    assert before == after
    [stored] = after["clinical_files"]  # the file's bytes came back exactly
    assert any(isinstance(v, bytes) and b"\x00\xff synthetic" in v for v in stored)


async def test_a_backup_opens_only_whole_and_with_its_key(
    source: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(backup, "CHUNK", 256)  # many chunks: the boundaries are checked too
    public, private = backup.keygen()
    sealed = await _backup(source, public)
    assert backup.check(io.BytesIO(sealed), private).counts

    _, stranger = backup.keygen()
    with pytest.raises(backup.BackupError, match="does not open"):
        backup.check(io.BytesIO(sealed), stranger)
    altered = bytearray(sealed)
    altered[len(altered) // 2] ^= 0x01
    with pytest.raises(backup.BackupError):
        backup.check(io.BytesIO(bytes(altered)), private)
    # Cut at a chunk boundary: the chunk before is not marked last, so it does not open.
    header = len(backup.MAGIC) + 32 + backup.PREFIX_BYTES
    offset, boundaries = header, []
    while offset < len(sealed):
        offset += 4 + int.from_bytes(sealed[offset : offset + 4], "big")
        boundaries.append(offset)
    with pytest.raises(backup.BackupError):
        backup.check(io.BytesIO(sealed[: boundaries[-2]]), private)
    with pytest.raises(backup.BackupError):
        backup.check(io.BytesIO(sealed[:-7]), private)
    with pytest.raises(backup.BackupError, match="not an agency backup"):
        backup.check(io.BytesIO(b"PK\x03\x04 a zip"), private)


def test_keys_and_values() -> None:
    public, private = backup.keygen()
    assert public.startswith(backup.PUBLIC_PREFIX) and private.startswith(backup.PRIVATE_PREFIX)
    with pytest.raises(backup.BackupError, match="not a key of this kind"):
        backup.public_key(private)
    with pytest.raises(backup.BackupError, match="damaged"):
        backup.private_key(backup.PRIVATE_PREFIX + "AAAA")
    values = [
        None,
        True,
        3,
        2.5,
        "texto ñ",
        datetime(2026, 10, 14, 9, 30, tzinfo=UTC),
        date(1990, 5, 4),
        Decimal("35.50"),
        b"\x00\xff",
        {"$dt": "a JSON column that looks like a tag"},
        [1, {"a": 2}],
    ]
    for value in values:
        assert backup.decode(backup.encode(value)) == value


def test_the_command_line(
    source: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli, "get_settings", lambda: source)
    key_file = tmp_path / "backup-private.txt"
    assert cli.main(["backup", "keygen", "--private-key-file", str(key_file)]) == 0
    public = capsys.readouterr().out.strip()
    assert public.startswith(backup.PUBLIC_PREFIX) and "private" not in public
    with pytest.raises(SystemExit):  # never overwrite what may open older backups
        cli.main(["backup", "keygen", "--private-key-file", str(key_file)])
    out = tmp_path / "copia.agbk"
    assert cli.main(["backup", "create", "--out", str(out), "--recipient", public]) == 0
    assert out.exists() and not out.with_name(out.name + ".partial").exists()
    check = ["backup", "restore", "--in", str(out), "--private-key-file", str(key_file)]
    assert cli.main([*check, "--check"]) == 0
    assert '"crm_patients": 1' in capsys.readouterr().out
    _, other = backup.keygen()
    key_file.write_text(other, encoding="ascii")
    assert cli.main([*check, "--check"]) == 1  # another key: a clear message, no traceback
    assert "does not open" in capsys.readouterr().err


def test_the_daily_runner_names_and_keeps_the_newest(tmp_path: Path) -> None:
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "run_backup", Path(__file__).parents[1] / "deploy" / "backup" / "run_backup.py"
    )
    assert spec is not None and spec.loader is not None
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    for day in range(1, 36):
        name = runner.backup_name(datetime(2026, 9, day % 30 + 1, day % 24, tzinfo=UTC))
        (tmp_path / name).write_bytes(b"x")
    (tmp_path / "notes.txt").write_text("not a backup")
    removed = runner.prune(tmp_path, keep=30)
    assert len(removed) == 5 and len(list(tmp_path.glob(runner.PATTERN))) == 30
    assert (tmp_path / "notes.txt").exists()
    kept = sorted(p.name for p in tmp_path.glob(runner.PATTERN))
    assert all(old.name < kept[0] for old in removed)
