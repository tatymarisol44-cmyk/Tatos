"""The encrypted backup (RF-PRV-04): a restore drill (create, restore into an empty
database, every row identical), and a backup that does not open with another key, when
altered, or when cut short. Synthetic data only."""

from __future__ import annotations

import io
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import MetaData, select, text

import orchestrator.backup as backup
import orchestrator.cli as cli
from orchestrator.api.app import create_app
from orchestrator.catalog import Catalog
from orchestrator.config import Settings
from orchestrator.db import Database
from orchestrator.llm import FakeLLM
from orchestrator.schema import owned_tables, prepare
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
        uuid.UUID("12345678-1234-5678-1234-567812345678"),
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


async def test_only_the_applications_tables_are_kept(source: Settings, tmp_path: Path) -> None:
    """A database may hold tables that are not the platform's (a provider's sample tables):
    the backup leaves them out, names them, and the restore does not need them."""
    db = Database(source)
    try:
        async with db.engine.begin() as conn:
            await conn.execute(text("CREATE TABLE goals (id TEXT PRIMARY KEY, title TEXT)"))
            await conn.execute(text("INSERT INTO goals VALUES ('g1', 'not ours')"))
        public, code = backup.recovery_code()
        out = io.BytesIO()
        summary = await backup.create(db.engine, out, public, only=owned_tables())
    finally:
        await db.close()
    assert summary.skipped == ["goals"] and "goals" not in summary.counts
    assert backup.check(io.BytesIO(out.getvalue()), code).counts == summary.counts
    target = await _empty(source, tmp_path, "target.db")
    restored = Database(target)
    try:
        loaded = await backup.restore(restored.engine, io.BytesIO(out.getvalue()), code)
    finally:
        await restored.close()
    assert loaded.counts["crm_patients"] == 1


def test_a_failed_backup_leaves_no_file(
    source: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "get_settings", lambda: source)

    async def broken(*args: Any, **kwargs: Any) -> None:
        raise backup.BackupError("a value of type Point cannot be backed up")

    monkeypatch.setattr(backup, "create", broken)
    public, _ = backup.recovery_code()
    backups = tmp_path / "backups"
    backups.mkdir()
    out = backups / "copia.agbk"
    assert cli.main(["backup", "create", "--out", str(out), "--recipient", public]) == 1
    assert list(backups.iterdir()) == []


def test_a_recovery_code_is_short_safe_to_copy_and_catches_typos() -> None:
    public, code = backup.recovery_code()
    groups = code.split("-")
    assert len(groups) == 7 and all(len(g) == 4 for g in groups)
    assert not set(code.replace("-", "")) - set(backup.CROCKFORD)  # no O, I, L or U
    opened = backup.private_key(code).public_key().public_bytes_raw()
    assert backup.public_key(public).public_bytes_raw() == opened
    # Written by hand: lower case, spaces, O for zero and I or L for one still work.
    sloppy = code.lower().replace("-", " ").replace("0", "o").replace("1", "l")
    assert backup.private_key(sloppy).public_key().public_bytes_raw() == opened
    swapped = ("Z" if code[0] != "Z" else "Y") + code[1:]
    with pytest.raises(backup.BackupError, match="typo"):
        backup.private_key(swapped)
    with pytest.raises(backup.BackupError, match="7 groups of 4"):
        backup.private_key(code[:-5])
    # The sheet holds the code with instructions; the code is found in it.
    sheet = backup.recovery_sheet(code)
    assert code in sheet and "DOS veces" in sheet and backup.identity(sheet) == code
    assert backup.identity(f"nota\n{code}\n") == code


async def test_secrets_travel_inside_the_encryption(source: Settings) -> None:
    public, code = backup.recovery_code()
    db = Database(source)
    try:
        out = io.BytesIO()
        await backup.create(db.engine, out, public, {"PSEUDONYM_KEY": "synthetic-pseudonym"})
    finally:
        await db.close()
    assert b"synthetic-pseudonym" not in out.getvalue()
    summary = backup.check(io.BytesIO(out.getvalue()), code)
    assert summary.secrets == {"PSEUDONYM_KEY": "synthetic-pseudonym"}
    assert summary.to_dict()["secrets"] == ["PSEUDONYM_KEY"]
    assert "synthetic-pseudonym" not in str(summary.to_dict()) + repr(summary)


def test_the_command_line_with_a_recovery_code(
    source: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli, "get_settings", lambda: source)
    monkeypatch.setenv("PSEUDONYM_KEY", "synthetic-pseudonym")
    sheet = tmp_path / "CODIGO-RESPALDO.txt"
    assert cli.main(["backup", "keygen", "--recovery-code-file", str(sheet)]) == 0
    public = capsys.readouterr().out.strip()
    code = backup.identity(sheet.read_text(encoding="utf-8"))
    assert code not in public
    out = tmp_path / "copia.agbk"
    create = ["backup", "create", "--out", str(out), "--recipient", public]
    assert cli.main([*create, "--with-secret", "PSEUDONYM_KEY"]) == 0
    assert "synthetic-pseudonym" not in capsys.readouterr().out
    with pytest.raises(SystemExit):  # a secret that is not set is an error, not a silent gap
        cli.main([*create, "--with-secret", "NOT_SET_ANYWHERE"])
    capsys.readouterr()
    secrets_out = tmp_path / "secretos.env"
    check = ["backup", "restore", "--in", str(out), "--key-file", str(sheet), "--check"]
    assert cli.main([*check, "--secrets-out", str(secrets_out)]) == 0
    assert secrets_out.read_text(encoding="utf-8") == "PSEUDONYM_KEY=synthetic-pseudonym\n"
    assert "synthetic-pseudonym" not in capsys.readouterr().out
    # Typed from the paper at a prompt when no file is given.
    monkeypatch.setattr("getpass.getpass", lambda prompt="": code.lower())
    assert cli.main(["backup", "restore", "--in", str(out), "--check"]) == 0


def test_the_first_run_makes_the_pair_once(tmp_path: Path) -> None:
    runner = _runner()
    secrets = tmp_path / "agency-render.env"
    secrets.write_text("SUPABASE_PASSWORD=x\n", encoding="utf-8")
    values: dict[str, str] = {}
    sheet = tmp_path / "CODIGO-RESPALDO.txt"
    public = runner.new_key(secrets, values, sheet)
    assert f"BACKUP_PUBLIC_KEY={public}" in secrets.read_text(encoding="utf-8")
    code = backup.identity(sheet.read_text(encoding="utf-8"))
    assert backup.private_key(code).public_key().public_bytes_raw() == (
        backup.public_key(public).public_bytes_raw()
    )
    with pytest.raises(runner.setup.SetupError, match="already set"):
        runner.new_key(secrets, values, tmp_path / "other.txt")
    with pytest.raises(runner.setup.SetupError, match="exists"):
        runner.new_key(secrets, {}, sheet)


def _runner() -> Any:
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "run_backup", Path(__file__).parents[1] / "deploy" / "backup" / "run_backup.py"
    )
    assert spec is not None and spec.loader is not None
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    return runner


def test_the_daily_runner_names_and_keeps_the_newest(tmp_path: Path) -> None:
    runner = _runner()
    for day in range(1, 36):
        name = runner.backup_name(datetime(2026, 9, day % 30 + 1, day % 24, tzinfo=UTC))
        (tmp_path / name).write_bytes(b"x")
    (tmp_path / "notes.txt").write_text("not a backup")
    removed = runner.prune(tmp_path, keep=30)
    assert len(removed) == 5 and len(list(tmp_path.glob(runner.PATTERN))) == 30
    assert (tmp_path / "notes.txt").exists()
    kept = sorted(p.name for p in tmp_path.glob(runner.PATTERN))
    assert all(old.name < kept[0] for old in removed)
