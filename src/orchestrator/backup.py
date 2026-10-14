"""An encrypted copy of the database, kept away from its provider (SRS RF-PRV-04).

Supabase's free plan keeps backups the practice cannot download, so the practice keeps
its own (docs/runbooks/backup.md):

* **Keys.** `agency backup keygen` makes a key pair once. The public key can sit anywhere
  (the scheduled task's settings): it only locks. The private half stays with the owner,
  offline: a key file on a USB drive, or a **recovery code** written on paper (28 Crockford
  base32 characters in groups of 4, no O, I or L to misread, the last two a checksum that
  catches a copying mistake; the key is derived from its 128 random bits with HKDF).
  Without it no backup opens, not even for whoever runs the task or holds the file.
* **Create.** `agency backup create` reads every table in the database (the conversation
  checkpoints and the migration version included), as JSON lines, gzipped, then
  encrypted: X25519 with a fresh key per backup, HKDF-SHA256, and AES-256-GCM in 1 MiB
  chunks whose nonce counts the chunk and marks the last one (the STREAM construction),
  so a file cut short, reordered or altered does not open.
* **Restore.** `agency backup restore` loads a backup into an empty database migrated to
  the same version; `--check` only decrypts it and counts the rows. A restore drill runs
  in CI (tests/test_backup.py).
* **Secrets that live outside the database** can travel inside the encrypted backup
  (`--with-secret PSEUDONYM_KEY`, which unseals the inbox and matches opt-outs), so the
  one recovery code restores everything; `restore --secrets-out` writes them to a file.
  The provider's own backups never hold them."""

from __future__ import annotations

import base64
import gzip
import hashlib
import io
import json
import os
import re
import struct
from collections.abc import AsyncIterator, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import IO, Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from sqlalchemy import MetaData, Table, func, insert, select, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from orchestrator.db import utcnow

MAGIC = b"AGBK1\n"
PREFIX_BYTES = 7  # nonce = prefix (7) + chunk counter (4) + last-chunk flag (1)
CHUNK = 1 << 20
FORMAT = "agency-backup"
VERSION = 1
BATCH = 500
# Written by the schema tools themselves, never restored over.
BOOKKEEPING = frozenset({"alembic_version", "checkpoint_migrations"})
PUBLIC_PREFIX = "agency-backup-public:"
PRIVATE_PREFIX = "agency-backup-private:"


class BackupError(ValueError):
    """A backup that does not open (wrong key, altered, cut short) or does not fit."""


# --- keys -------------------------------------------------------------------------------


def keygen() -> tuple[str, str]:
    """(public, private) as printable lines."""
    private = X25519PrivateKey.generate()
    raw_private = private.private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )
    return _public_line(private), PRIVATE_PREFIX + base64.b64encode(raw_private).decode()


def _key(value: str, prefix: str) -> bytes:
    value = value.strip()
    if not value.startswith(prefix):
        raise BackupError(f"not a key of this kind: expected it to start with {prefix!r}")
    try:
        raw = base64.b64decode(value.removeprefix(prefix), validate=True)
    except ValueError as exc:
        raise BackupError("the key is damaged") from exc
    if len(raw) != 32:
        raise BackupError("the key is damaged")
    return raw


def public_key(value: str) -> X25519PublicKey:
    return X25519PublicKey.from_public_bytes(_key(value, PUBLIC_PREFIX))


def private_key(value: str) -> X25519PrivateKey:
    """From a key file's line or from a recovery code."""
    if value.strip().startswith(PRIVATE_PREFIX):
        return X25519PrivateKey.from_private_bytes(_key(value, PRIVATE_PREFIX))
    return _from_seed(_seed(value))


def _public_line(private: X25519PrivateKey) -> str:
    raw = private.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    return PUBLIC_PREFIX + base64.b64encode(raw).decode()


# --- the recovery code: the private key, short enough to copy by hand ----------------------

CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"  # no I, L, O, U: nothing to misread
SEED_BYTES = 16  # 128 bits: far beyond any guessing, and 26 characters
CODE_CHARS = 28  # 26 for the seed, 2 for the checksum


def _base32(data: bytes, chars: int) -> str:
    number = int.from_bytes(data, "big")
    return "".join(CROCKFORD[(number >> (5 * i)) & 31] for i in reversed(range(chars)))


def _checksum(seed: bytes) -> str:
    return _base32(hashlib.sha256(seed).digest()[:2], 4)[-2:]


def _from_seed(seed: bytes) -> X25519PrivateKey:
    raw = HKDF(
        algorithm=hashes.SHA256(), length=32, salt=None, info=b"agency-backup-recovery-v1"
    ).derive(seed)
    return X25519PrivateKey.from_private_bytes(raw)


def recovery_code() -> tuple[str, str]:
    """(public key line, recovery code `XXXX-XXXX-...`): a new pair."""
    seed = os.urandom(SEED_BYTES)
    code = _base32(seed, 26) + _checksum(seed)
    return _public_line(_from_seed(seed)), "-".join(
        code[i : i + 4] for i in range(0, CODE_CHARS, 4)
    )


def _seed(code: str) -> bytes:
    clean = code.upper().translate(str.maketrans("OIL", "011", "- \t\r\n"))
    if len(clean) != CODE_CHARS or any(c not in CROCKFORD for c in clean):
        raise BackupError("a recovery code has 28 letters and digits, in 7 groups of 4")
    number = 0
    for char in clean[:26]:
        number = number * 32 + CROCKFORD.index(char)
    if number >> (8 * SEED_BYTES):
        raise BackupError("the recovery code has a typo: check it group by group")
    seed = number.to_bytes(SEED_BYTES, "big")
    if _checksum(seed) != clean[26:]:
        raise BackupError("the recovery code has a typo: check it group by group")
    return seed


_CODE = re.compile(r"\b[0-9A-Za-z]{4}(?:-[0-9A-Za-z]{4}){6}\b")


def identity(text: str) -> str:
    """The key in a key file, or the recovery code in a recovery sheet or a typed note."""
    for line in text.splitlines():
        if line.strip().startswith(PRIVATE_PREFIX):
            return line.strip()
    found = _CODE.search(text)
    return found.group(0) if found else text.strip()


def recovery_sheet(code: str) -> str:
    """What the owner reads once, copies by hand, then deletes (Spanish: it is for her)."""
    return (
        "CÓDIGO DE RECUPERACIÓN DE LOS RESPALDOS DEL CONSULTORIO\n"
        "========================================================\n\n"
        f"    {code}\n\n"
        "1. Cópialo a mano en un papel, DOS veces. Revisa grupo por grupo.\n"
        "   (No lleva la letra O ni la I: lo que parece O es un cero, lo que parece I es un uno.)\n"
        "2. Guarda cada papel en un lugar distinto (por ejemplo, tu casa y la de un familiar).\n"
        "3. Cierra este archivo, bórralo y vacía la papelera.\n\n"
        "Sin este código ningún respaldo se puede abrir, y nadie más lo tiene: ni el\n"
        "consultorio, ni la plataforma, ni el asistente que te ayudó a crearlo.\n"
        "Con él y una copia del respaldo se recupera todo, incluidos los mensajes.\n"
    )


def _derive(shared: bytes, ephemeral: bytes, recipient: bytes) -> AESGCM:
    key = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=ephemeral + recipient,
        info=b"agency-backup-v1",
    ).derive(shared)
    return AESGCM(key)


def _nonce(prefix: bytes, counter: int, last: bool) -> bytes:
    return prefix + struct.pack(">I", counter) + (b"\x01" if last else b"\x00")


# --- the encrypted stream ---------------------------------------------------------------


class _Sealing(io.RawIOBase):
    """A writable stream that encrypts what it receives, chunk by chunk."""

    def __init__(self, out: IO[bytes], recipient: X25519PublicKey) -> None:
        ephemeral = X25519PrivateKey.generate()
        ephemeral_public = ephemeral.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
        recipient_raw = recipient.public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
        self._aead = _derive(ephemeral.exchange(recipient), ephemeral_public, recipient_raw)
        self._prefix = os.urandom(PREFIX_BYTES)
        self._header = MAGIC + ephemeral_public + self._prefix
        self._out = out
        self._buffer = bytearray()
        self._counter = 0
        out.write(self._header)

    def writable(self) -> bool:
        return True

    def write(self, data: Any) -> int:
        self._buffer.extend(data)
        while len(self._buffer) > CHUNK:  # keep at least one chunk back for the last flag
            self._emit(bytes(self._buffer[:CHUNK]), last=False)
            del self._buffer[:CHUNK]
        return len(data)

    def _emit(self, plain: bytes, last: bool) -> None:
        sealed = self._aead.encrypt(_nonce(self._prefix, self._counter, last), plain, self._header)
        self._out.write(struct.pack(">I", len(sealed)) + sealed)
        self._counter += 1

    def finish(self) -> None:
        self._emit(bytes(self._buffer), last=True)
        self._buffer.clear()


def _opened(source: IO[bytes], identity: X25519PrivateKey) -> Iterator[bytes]:
    """The decrypted chunks, in order; raises BackupError if anything does not fit."""
    header = source.read(len(MAGIC) + 32 + PREFIX_BYTES)
    if len(header) != len(MAGIC) + 32 + PREFIX_BYTES or not header.startswith(MAGIC):
        raise BackupError("not an agency backup")
    ephemeral = header[len(MAGIC) : len(MAGIC) + 32]
    prefix = header[len(MAGIC) + 32 :]
    recipient = identity.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    aead = _derive(
        identity.exchange(X25519PublicKey.from_public_bytes(ephemeral)), ephemeral, recipient
    )
    counter = 0
    pending = _read_chunk(source)
    if pending is None:
        raise BackupError("the backup is cut short")
    while pending is not None:
        following = _read_chunk(source)
        last = following is None
        try:
            yield aead.decrypt(_nonce(prefix, counter, last), pending, header)
        except InvalidTag as exc:
            raise BackupError(
                "the backup does not open: another key, or the file was altered or cut short"
            ) from exc
        counter += 1
        pending = following


def _read_chunk(source: IO[bytes]) -> bytes | None:
    size = source.read(4)
    if not size:
        return None
    if len(size) != 4:
        raise BackupError("the backup is cut short")
    (length,) = struct.unpack(">I", size)
    if length > CHUNK + 16:
        raise BackupError("the backup is damaged")
    data = source.read(length)
    if len(data) != length:
        raise BackupError("the backup is cut short")
    return data


class _Chunks(io.RawIOBase):
    """The decrypted chunks as one readable stream (for gzip)."""

    def __init__(self, chunks: Iterator[bytes]) -> None:
        self._chunks = chunks
        self._left = b""

    def readable(self) -> bool:
        return True

    def readinto(self, target: Any) -> int:
        while not self._left:
            try:
                self._left = next(self._chunks)
            except StopIteration:
                return 0
        n = min(len(target), len(self._left))
        target[:n] = self._left[:n]
        self._left = self._left[n:]
        return n


# --- values -------------------------------------------------------------------------------


def encode(value: Any) -> Any:
    """A column value as JSON. Anything that is not a JSON scalar is tagged, so a JSON
    column holding a dict is never mistaken for a tagged value."""
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, datetime):
        return {"$dt": value.isoformat()}
    if isinstance(value, date):
        return {"$date": value.isoformat()}
    if isinstance(value, Decimal):
        return {"$dec": str(value)}
    if isinstance(value, bytes | bytearray | memoryview):
        return {"$b64": base64.b64encode(bytes(value)).decode()}
    if isinstance(value, dict | list):
        return {"$json": value}
    raise BackupError(f"a value of type {type(value).__name__} cannot be backed up")


def decode(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    [(tag, raw)] = value.items()
    if tag == "$dt":
        return datetime.fromisoformat(raw)
    if tag == "$date":
        return date.fromisoformat(raw)
    if tag == "$dec":
        return Decimal(raw)
    if tag == "$b64":
        return base64.b64decode(raw)
    if tag == "$json":
        return raw
    raise BackupError(f"unknown value tag {tag!r}")


# --- create and restore ---------------------------------------------------------------------


@dataclass
class Summary:
    created_at: str = ""
    revision: str | None = None
    counts: dict[str, int] = field(default_factory=dict)
    # Secrets carried inside the encryption; the summary shows their names only.
    secrets: dict[str, str] = field(default_factory=dict, repr=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "created_at": self.created_at,
            "revision": self.revision,
            "tables": len(self.counts),
            "rows": sum(self.counts.values()),
            "counts": self.counts,
            "secrets": sorted(self.secrets),
        }


async def _tables(conn: AsyncConnection) -> list[Table]:
    reflected = MetaData()
    await conn.run_sync(lambda sync: reflected.reflect(bind=sync))
    return list(reflected.sorted_tables)


async def _revision(conn: AsyncConnection, names: set[str]) -> str | None:
    if "alembic_version" not in names:
        return None
    return (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalar()


async def _rows(conn: AsyncConnection, table: Table) -> AsyncIterator[dict[str, Any]]:
    result = await conn.stream(select(table).execution_options(yield_per=BATCH))
    async for row in result:
        yield dict(row._mapping)


async def create(
    engine: AsyncEngine,
    out: IO[bytes],
    recipient: str,
    secrets: Mapping[str, str] | None = None,
) -> Summary:
    """Write an encrypted backup of every table to `out`, with `secrets` inside it."""
    key = public_key(recipient)
    sealing = _Sealing(out, key)
    summary = Summary(created_at=utcnow().isoformat(), secrets=dict(secrets or {}))
    with gzip.GzipFile(fileobj=sealing, mode="wb", mtime=0) as packed:

        def line(doc: dict[str, Any]) -> None:
            packed.write(json.dumps(doc, ensure_ascii=False, separators=(",", ":")).encode())
            packed.write(b"\n")

        async with engine.connect() as conn:
            tables = await _tables(conn)
            summary.revision = await _revision(conn, {t.name for t in tables})
            line(
                {
                    "format": FORMAT,
                    "version": VERSION,
                    "created_at": summary.created_at,
                    "revision": summary.revision,
                    "tables": [t.name for t in tables],
                    "secrets": summary.secrets,
                }
            )
            for table in tables:
                count = 0
                async for row in _rows(conn, table):
                    line({"t": table.name, "r": {k: encode(v) for k, v in row.items()}})
                    count += 1
                summary.counts[table.name] = count
        line({"end": True, "counts": summary.counts})
    sealing.finish()
    return summary


def _lines(source: IO[bytes], identity: str) -> Iterator[dict[str, Any]]:
    stream = io.BufferedReader(_Chunks(_opened(source, private_key(identity))))
    with gzip.GzipFile(fileobj=stream, mode="rb") as unpacked:
        try:
            for raw in unpacked:
                yield json.loads(raw)
        except (OSError, EOFError, ValueError) as exc:
            if isinstance(exc, BackupError):
                raise
            raise BackupError("the backup is damaged") from exc


def check(source: IO[bytes], identity: str) -> Summary:
    """Decrypt and count without touching any database."""
    summary = Summary()
    counted: dict[str, int] = {}
    ended = False
    for doc in _lines(source, identity):
        if "format" in doc:
            if doc["format"] != FORMAT or doc["version"] != VERSION:
                raise BackupError("a backup of another format or version")
            summary.created_at, summary.revision = doc["created_at"], doc["revision"]
            summary.secrets = doc.get("secrets", {})
        elif "t" in doc:
            counted[doc["t"]] = counted.get(doc["t"], 0) + 1
        elif doc.get("end"):
            ended = True
            summary.counts = doc["counts"]
    if not ended or {k: v for k, v in summary.counts.items() if v} != counted:
        raise BackupError("the backup is incomplete")
    return summary


async def restore(engine: AsyncEngine, source: IO[bytes], identity: str) -> Summary:
    """Load a backup into an empty database migrated to the same revision."""
    summary = Summary()
    async with engine.begin() as conn:
        tables = {t.name: t for t in await _tables(conn)}
        current = await _revision(conn, set(tables))
        pending: dict[str, list[dict[str, Any]]] = {}
        loaded: dict[str, int] = {}
        ended = False

        async def flush(name: str) -> None:
            rows = pending.pop(name, [])
            if rows:
                await conn.execute(insert(tables[name]), rows)
                loaded[name] = loaded.get(name, 0) + len(rows)

        for doc in _lines(source, identity):
            if "format" in doc:
                if doc["format"] != FORMAT or doc["version"] != VERSION:
                    raise BackupError("a backup of another format or version")
                if doc["revision"] != current:
                    raise BackupError(
                        f"the backup is at schema {doc['revision']} and this database at "
                        f"{current}: run `agency db upgrade` to the same version first"
                    )
                missing = [n for n in doc["tables"] if n not in tables]
                if missing:
                    raise BackupError(f"tables missing here: {missing}")
                for name in doc["tables"]:
                    if name in BOOKKEEPING:
                        continue
                    used = (
                        await conn.execute(select(func.count()).select_from(tables[name]))
                    ).scalar_one()
                    if used:
                        raise BackupError(f"restore into an empty database: {name} has rows")
                summary.created_at, summary.revision = doc["created_at"], doc["revision"]
                summary.secrets = doc.get("secrets", {})
            elif "t" in doc:
                name = doc["t"]
                if name in BOOKKEEPING:
                    continue
                pending.setdefault(name, []).append({k: decode(v) for k, v in doc["r"].items()})
                if len(pending[name]) >= BATCH:
                    await flush(name)
            elif doc.get("end"):
                for name in list(pending):
                    await flush(name)
                expected = {k: v for k, v in doc["counts"].items() if v and k not in BOOKKEEPING}
                if loaded != expected:
                    raise BackupError("the backup is incomplete")  # rolled back
                summary.counts = loaded
                ended = True
        if not ended:
            raise BackupError("the backup is incomplete")
        if conn.dialect.name == "postgresql":
            await _reset_sequences(conn, list(tables.values()))
    return summary


async def _reset_sequences(conn: AsyncConnection, tables: list[Table]) -> None:
    """Integer keys drawn from a sequence continue after the restored rows."""
    for table in tables:
        for column in table.primary_key.columns:
            sequence = (
                await conn.execute(
                    text("SELECT pg_get_serial_sequence(:t, :c)"),
                    {"t": table.name, "c": column.name},
                )
            ).scalar()
            if sequence:
                highest = select(func.max(column)).scalar_subquery()
                await conn.execute(select(func.setval(sequence, func.coalesce(highest, 1))))
