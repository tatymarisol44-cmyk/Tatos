"""Staff sign-in with e-mail, password and an authenticator app (ADR 0024).

- **Who.** A staff principal gets an e-mail login when the administration adds the person
  in Equipo, or when the person adds one to their own key account. The answer is a
  personal link, sent by WhatsApp like the patients' links, to choose a password and add
  the practice to an authenticator app (Google Authenticator, Microsoft Authenticator...).
- **Passwords** are hashed with scrypt (N=2^13, r=8, p=10: OWASP's 8 MiB profile, sized for
  the free instance's 512 MB), a random salt each, compared in constant time. An unknown
  e-mail costs the same work as a wrong password.
- **The second factor** is a TOTP code (RFC 6238: HMAC-SHA1, 30-second steps, 6 digits).
  Its secret is sealed at rest (sealing.py, AES-256-GCM, its own purpose label). A code is
  accepted for the current step or the one before or after, and each step only once.
- **Sign-in.** E-mail and password open a sign-in valid 10 minutes with 5 tries for the
  code; the code gives a session key `ss_...` (auth.py) for 12 hours, or 30 days when the
  person asks to be remembered on that computer.
- **The password and the authenticator are saved together**, when the first code from
  the app confirms a link: knowing a password never lets someone add their own app.
- **One remedy for everything.** A forgotten password or a lost phone is solved with a new
  link from Equipo (valid 72 hours). Using it sets a new password and a new authenticator,
  both at once, and ends every open session. Until it is used, the old ones keep working.
- **Locks.** 5 wrong passwords lock the login for 15 minutes.
- **Nothing tells whether an e-mail exists.** A wrong e-mail and a wrong password answer
  alike.
- Every step is audited by principal id, never with a password, a code or a key."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import re
import secrets
import time
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import quote

import segno
from sqlalchemy import (
    Column,
    DateTime,
    Integer,
    String,
    Table,
    Text,
    and_,
    case,
    delete,
    func,
    insert,
    or_,
    select,
    update,
)

from orchestrator.auth import STAFF, PrincipalStore, Role, principals, staff_sessions
from orchestrator.config import Settings
from orchestrator.db import Database, aware, metadata, utcnow
from orchestrator.governance import AuditLog
from orchestrator.sealing import Sealer, SealError

staff_logins = Table(
    "staff_logins",
    metadata,
    Column("tenant", String(64), primary_key=True),
    Column("principal_id", String(128), primary_key=True),
    # Lower-cased. Unique across the deployment: signing in names no practice.
    Column("email", String(254), nullable=False, unique=True, index=True),
    Column("display_name", String(120), nullable=False),
    Column("password_hash", String(200), nullable=True),  # None until a link is used
    Column("password_set_at", DateTime(timezone=True), nullable=True),
    Column("totp_sealed", Text, nullable=True),  # the authenticator's secret, sealed
    Column("totp_last_step", Integer, nullable=True),  # a code's step is used once
    Column("failed_attempts", Integer, nullable=False, default=0),
    Column("locked_until", DateTime(timezone=True), nullable=True),
    Column("last_sign_in_at", DateTime(timezone=True), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("created_by", String(128), nullable=False),
)

# A personal link (link), the step that adds the authenticator after it (enroll), and a
# sign-in waiting for its code (sign_in).
login_challenges = Table(
    "login_challenges",
    metadata,
    Column("challenge_id", String(32), primary_key=True),
    Column("tenant", String(64), nullable=False),
    Column("principal_id", String(128), nullable=False, index=True),
    Column("purpose", String(16), nullable=False),
    Column("secret_hash", String(64), nullable=False, index=True),
    Column("payload_sealed", Text, nullable=True),  # enroll: the new password and secret
    Column("attempts", Integer, nullable=False, default=0),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Column("used_at", DateTime(timezone=True), nullable=True),
)

LINK, ENROLL, SIGN_IN = "link", "enroll", "sign_in"
LINK_HOURS = 72
STEP_MINUTES = 10
CODE_TRIES = 5
LOCK_AFTER = 5
LOCK_MINUTES = 15
MIN_PASSWORD = 10
MAX_PASSWORD = 200
STEP_SECONDS = 30
# scrypt: OWASP's 8 MiB profile (N=2^13, r=8, p=10).
_N, _R, _P = 2**13, 8, 10
_MAXMEM = 32 * 1024 * 1024
EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,189}\.[^@\s]{2,}$")
COMMON = {
    "1234567890", "12345678910", "contraseña", "contrasena1", "password123", "qwertyuiop",
    "consultorio", "consultorio1", "psicologia", "psicologia1", "0987654321",
}  # fmt: skip


class SignInError(Exception):
    """Wrong e-mail, password or code, or a locked login: one answer for all of them."""


class LinkError(Exception):
    """A link or an authenticator step that is unknown, used or expired."""


class WeakPassword(ValueError):
    """The password does not meet the rules; the message says which one."""


def normalize(email: str) -> str:
    return email.strip().lower()


def valid_email(email: str) -> bool:
    return len(email) <= 254 and EMAIL.fullmatch(email) is not None


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode(), salt=salt, n=_N, r=_R, p=_P, maxmem=_MAXMEM, dklen=32
    )
    b64 = base64.b64encode
    return f"scrypt${_N}${_R}${_P}${b64(salt).decode()}${b64(digest).decode()}"


def check_password(password: str, stored: str | None) -> bool:
    """Constant-time; with `stored` None it does the same work and answers False."""
    if stored is None:
        hash_password(password)
        return False
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        got = hashlib.scrypt(
            password.encode(),
            salt=base64.b64decode(salt),
            n=int(n),
            r=int(r),
            p=int(p),
            maxmem=_MAXMEM,
            dklen=32,
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(got, base64.b64decode(digest))


def password_problem(password: str, email: str) -> str | None:
    """Why this password is refused, in the person's language; None when it is fine."""
    if len(password) < MIN_PASSWORD:
        return f"La contraseña necesita al menos {MIN_PASSWORD} caracteres."
    if len(password) > MAX_PASSWORD:
        return f"La contraseña puede tener hasta {MAX_PASSWORD} caracteres."
    lowered = password.lower()
    if lowered in COMMON or len(set(password)) < 4:
        return "Esa contraseña es muy fácil de adivinar. Elige otra."
    local = email.partition("@")[0].lower()
    if len(local) >= 4 and local in lowered:
        return "La contraseña no puede contener tu correo."
    return None


# --- TOTP (RFC 6238) ----------------------------------------------------------------------


def _time() -> float:
    return time.time()  # one place, so tests can move the clock


def current_step() -> int:
    return int(_time() // STEP_SECONDS)


def totp_at(secret: bytes, step: int, digits: int = 6) -> str:
    mac = hmac.new(secret, step.to_bytes(8, "big"), "sha1").digest()
    offset = mac[-1] & 0x0F
    value = int.from_bytes(mac[offset : offset + 4], "big") & 0x7FFFFFFF
    return f"{value % 10**digits:0{digits}d}"


def matching_step(secret: bytes, code: str, after: int | None) -> int | None:
    """The step `code` belongs to (now, or one step either side), if not used before."""
    code = code.strip()
    now = current_step()
    for step in (now - 1, now, now + 1):
        if (after is None or step > after) and hmac.compare_digest(totp_at(secret, step), code):
            return step
    return None


def b32(secret: bytes) -> str:
    return base64.b32encode(secret).decode().rstrip("=")


def unb32(text: str) -> bytes:
    return base64.b32decode(text + "=" * (-len(text) % 8))


def qr_rows(text: str) -> list[str]:
    """The QR code of `text` as rows of "0"/"1", quiet zone included: the app draws it."""
    code = segno.make(text, error="m", micro=False)
    return ["".join("1" if dark else "0" for dark in row) for row in code.matrix_iter(border=4)]


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _at(value: Any) -> datetime:
    """A NOT NULL timestamp column, time-zone aware whatever the database returns."""
    when = aware(value)
    assert when is not None
    return when


def _member_id(email: str) -> str:
    """A readable principal id from the e-mail: ana.perez for ana.perez@gmail.com."""
    local = re.sub(r"[^a-z0-9._-]", "", email.partition("@")[0].lower()) or "persona"
    return local[:48]


class StaffLogins:
    def __init__(
        self,
        db: Database,
        audit: AuditLog,
        principal_store: PrincipalStore,
        settings: Settings,
    ) -> None:
        self.db = db
        self.audit = audit
        self.principals = principal_store
        self.settings = settings
        self.sealer = Sealer(settings.pseudonym_secret(), purpose=b"staff-totp-v1")

    # --- helpers --------------------------------------------------------------------------

    def _url(self, token: str) -> str:
        return f"{self.settings.public_base_url.rstrip('/')}/app#clave={token}"

    async def _login(self, conn: Any, *where: Any, lock: bool = False) -> Any:
        query = (
            select(staff_logins, principals.c.revoked_at, principals.c.roles, principals.c.kind)
            .select_from(
                staff_logins.join(
                    principals,
                    and_(
                        principals.c.tenant == staff_logins.c.tenant,
                        principals.c.id == staff_logins.c.principal_id,
                    ),
                )
            )
            .where(and_(*where))
        )
        if lock:
            query = query.with_for_update(of=principals)
        return (await conn.execute(query)).mappings().first()

    async def _row(self, tenant: str, pid: str) -> Any:
        async with self.db.engine.connect() as conn:
            return await self._login(
                conn, staff_logins.c.tenant == tenant, staff_logins.c.principal_id == pid
            )

    async def _challenge(
        self,
        tenant: str,
        pid: str,
        purpose: str,
        lifetime: timedelta,
        secret_hash: str,
        payload: str | None = None,
    ) -> str:
        challenge_id = secrets.token_hex(16)
        now = utcnow()
        async with self.db.engine.begin() as conn:
            row = await self._login(
                conn,
                staff_logins.c.tenant == tenant,
                staff_logins.c.principal_id == pid,
                lock=True,
            )
            if row is None or row["revoked_at"] is not None or row["kind"] != STAFF:
                if purpose == SIGN_IN:
                    raise SignInError()
                raise LinkError()
            # One open challenge of each kind per person: a new one replaces the old.
            await conn.execute(
                update(login_challenges)
                .where(
                    and_(
                        login_challenges.c.tenant == tenant,
                        login_challenges.c.principal_id == pid,
                        login_challenges.c.purpose == purpose,
                        login_challenges.c.used_at.is_(None),
                    )
                )
                .values(used_at=now)
            )
            await conn.execute(
                insert(login_challenges).values(
                    challenge_id=challenge_id,
                    tenant=tenant,
                    principal_id=pid,
                    purpose=purpose,
                    secret_hash=secret_hash,
                    payload_sealed=payload,
                    attempts=0,
                    created_at=now,
                    expires_at=now + lifetime,
                )
            )
        return challenge_id

    async def _open(self, challenge_id: str, purpose: str) -> Any:
        async with self.db.engine.connect() as conn:
            return (
                (
                    await conn.execute(
                        select(login_challenges).where(
                            and_(
                                login_challenges.c.challenge_id == challenge_id,
                                login_challenges.c.purpose == purpose,
                            )
                        )
                    )
                )
                .mappings()
                .first()
            )

    async def _count_wrong(self, challenge_id: str, tenant: str, pid: str) -> None:
        async with self.db.engine.begin() as conn:
            await conn.execute(
                update(login_challenges)
                .where(login_challenges.c.challenge_id == challenge_id)
                .values(attempts=login_challenges.c.attempts + 1)
            )
            await self.audit.record_in(conn, tenant, pid, "auth.totp.wrong", f"principal/{pid}")

    def _session_length(self, remember: bool) -> timedelta:
        if remember:
            return timedelta(days=self.settings.staff_session_remember_days)
        return timedelta(hours=self.settings.staff_session_hours)

    # --- the team (administration) --------------------------------------------------------

    async def team(self, tenant: str) -> list[dict[str, Any]]:
        """Every staff member, with their e-mail login when they have one."""
        query = (
            select(
                principals.c.id,
                principals.c.roles,
                principals.c.created_at,
                principals.c.revoked_at,
                staff_logins.c.email,
                staff_logins.c.display_name,
                staff_logins.c.totp_sealed,
                staff_logins.c.last_sign_in_at,
            )
            .select_from(
                principals.outerjoin(
                    staff_logins,
                    and_(
                        staff_logins.c.tenant == principals.c.tenant,
                        staff_logins.c.principal_id == principals.c.id,
                    ),
                )
            )
            .where(and_(principals.c.tenant == tenant, principals.c.kind == STAFF))
            .order_by(principals.c.created_at)
        )
        async with self.db.engine.connect() as conn:
            rows = (await conn.execute(query)).mappings().all()
        out = []
        for r in rows:
            # Erased access profiles leave only the principal used by audit/clinical
            # authorship. They are not members to invite, recover or remove again.
            if r["revoked_at"] is not None and r["email"] is None:
                continue
            if r["revoked_at"] is not None:
                status = "revoked"
            elif r["email"] is None:
                status = "key_only"
            elif r["totp_sealed"] is None:
                status = "invited"
            else:
                status = "active"
            last = aware(r["last_sign_in_at"])
            out.append(
                {
                    "id": r["id"],
                    "name": r["display_name"] or r["id"],
                    "email": r["email"],
                    "roles": sorted(r["roles"]),
                    "status": status,
                    "last_sign_in_at": last.isoformat() if last else None,
                }
            )
        return out

    async def add_member(
        self, tenant: str, actor: str, name: str, email: str, roles: list[Role]
    ) -> dict[str, Any]:
        """A new staff member with an e-mail login, and their personal link."""
        email = normalize(email)
        if not valid_email(email):
            raise ValueError("invalid e-mail")
        if not roles:
            raise ValueError("a staff member needs at least one role")
        async with self.db.engine.connect() as conn:
            taken = (
                await conn.execute(
                    select(staff_logins.c.email).where(staff_logins.c.email == email)
                )
            ).first()
            ids = {
                r.id
                for r in await conn.execute(
                    select(principals.c.id).where(principals.c.tenant == tenant)
                )
            }
        if taken is not None:
            raise ValueError("e-mail already in use")
        base = _member_id(email)
        pid = base
        n = 2
        while pid in ids:
            pid, n = f"{base}{n}", n + 1
        # The staff key this creates is never shown: the person signs in with e-mail.
        await self.principals.create_staff(tenant, pid, roles, actor)
        await self._attach(tenant, pid, actor, email, name.strip()[:120] or pid)
        link = await self.link(tenant, pid, actor)
        member = next(m for m in await self.team(tenant) if m["id"] == pid)
        return {**member, **link}

    async def attach_own_email(
        self, tenant: str, pid: str, email: str, name: str | None = None
    ) -> dict[str, str]:
        """A staff member adds an e-mail login to their own key account; the answer is
        their personal link, to use right away."""
        email = normalize(email)
        if not valid_email(email):
            raise ValueError("invalid e-mail")
        async with self.db.engine.connect() as conn:
            mine = await self._login(
                conn, staff_logins.c.tenant == tenant, staff_logins.c.principal_id == pid
            )
            taken = (
                await conn.execute(
                    select(staff_logins.c.principal_id).where(staff_logins.c.email == email)
                )
            ).first()
        if mine is not None and mine["totp_sealed"] is not None:
            raise ValueError("this account already signs in with e-mail")
        if taken is not None and (mine is None or mine["email"] != email):
            raise ValueError("e-mail already in use")
        if mine is None:
            await self._attach(tenant, pid, pid, email, (name or pid).strip()[:120])
        elif mine["email"] != email:
            async with self.db.engine.begin() as conn:
                await conn.execute(
                    update(staff_logins)
                    .where(
                        and_(staff_logins.c.tenant == tenant, staff_logins.c.principal_id == pid)
                    )
                    .values(email=email)
                )
        return await self.link(tenant, pid, pid)

    async def _attach(self, tenant: str, pid: str, actor: str, email: str, name: str) -> None:
        async with self.db.engine.begin() as conn:
            principal = (
                await conn.execute(
                    select(principals.c.kind, principals.c.revoked_at)
                    .where(and_(principals.c.tenant == tenant, principals.c.id == pid))
                    .with_for_update()
                )
            ).first()
            if principal is None or principal.kind != STAFF or principal.revoked_at is not None:
                raise ValueError("this staff account is no longer active")
            await conn.execute(
                insert(staff_logins).values(
                    tenant=tenant,
                    principal_id=pid,
                    email=email,
                    display_name=name,
                    failed_attempts=0,
                    created_at=utcnow(),
                    created_by=actor,
                )
            )
            await self.audit.record_in(
                conn, tenant, actor, "auth.login.created", f"principal/{pid}"
            )

    async def link(self, tenant: str, pid: str, actor: str) -> dict[str, str]:
        """A personal link to choose a password and add an authenticator (invitation,
        forgotten password, lost phone). Raises KeyError for an unknown member."""
        row = await self._row(tenant, pid)
        if row is None or row["revoked_at"] is not None:
            raise KeyError(pid)
        token = secrets.token_urlsafe(32)
        try:
            await self._challenge(tenant, pid, LINK, timedelta(hours=LINK_HOURS), _sha(token))
        except LinkError as exc:
            raise KeyError(pid) from exc
        await self.audit.record(tenant, actor, "auth.link.created", f"principal/{pid}")
        expires = utcnow() + timedelta(hours=LINK_HOURS)
        return {"link": self._url(token), "link_expires_at": expires.isoformat()}

    # --- using a link: a password, then the authenticator -----------------------------------

    async def use_link(self, token: str, password: str) -> dict[str, Any]:
        """Check the link and the new password; answer the authenticator's secret to add.
        Nothing changes until the first code is confirmed (`enroll`). Raises LinkError or
        WeakPassword."""
        now = utcnow()
        async with self.db.engine.connect() as conn:
            link = (
                (
                    await conn.execute(
                        select(login_challenges).where(
                            and_(
                                login_challenges.c.secret_hash == _sha(token),
                                login_challenges.c.purpose == LINK,
                            )
                        )
                    )
                )
                .mappings()
                .first()
            )
            if link is None or link["used_at"] is not None or _at(link["expires_at"]) <= now:
                raise LinkError()
            row = await self._login(
                conn,
                staff_logins.c.tenant == link["tenant"],
                staff_logins.c.principal_id == link["principal_id"],
            )
        if row is None or row["revoked_at"] is not None:
            raise LinkError()
        problem = password_problem(password, row["email"])
        if problem:
            raise WeakPassword(problem)
        hashed = await asyncio.to_thread(hash_password, password)
        secret = secrets.token_bytes(20)
        tenant, pid = link["tenant"], link["principal_id"]
        payload = json.dumps(
            {"link": link["challenge_id"], "password": hashed, "totp": b32(secret)}
        )
        challenge_id = await self._challenge(
            tenant,
            pid,
            ENROLL,
            timedelta(minutes=STEP_MINUTES),
            "-",
            self.sealer.seal(tenant, payload),
        )
        issuer = self.settings.practice_display_name or "Consultorio"
        uri = (
            f"otpauth://totp/{quote(issuer)}:{quote(row['email'])}?secret={b32(secret)}"
            f"&issuer={quote(issuer)}&algorithm=SHA1&digits=6&period={STEP_SECONDS}"
        )
        return {
            "challenge": challenge_id,
            "secret": b32(secret),
            "uri": uri,
            "qr": qr_rows(uri),
            "issuer": issuer,
            "email": row["email"],
        }

    async def enroll(self, challenge_id: str, code: str, remember: bool) -> dict[str, str]:
        """The first code from the app confirms it: the new password and authenticator are
        saved, every old session ends, and the person is signed in. Raises SignInError
        (wrong code) or LinkError (the step or its link is used or expired)."""
        ch = await self._open(challenge_id, ENROLL)
        now = utcnow()
        if (
            ch is None
            or ch["used_at"] is not None
            or _at(ch["expires_at"]) <= now
            or ch["attempts"] >= CODE_TRIES
        ):
            raise LinkError()
        tenant, pid = ch["tenant"], ch["principal_id"]
        try:
            pending = json.loads(self.sealer.open(tenant, ch["payload_sealed"] or ""))
        except (SealError, ValueError) as exc:
            raise LinkError() from exc
        step = matching_step(unb32(pending["totp"]), code, None)
        if step is None:
            await self._count_wrong(challenge_id, tenant, pid)
            raise SignInError()
        async with self.db.engine.begin() as conn:
            for used_id in (challenge_id, pending["link"]):
                used = await conn.execute(
                    update(login_challenges)
                    .where(
                        and_(
                            login_challenges.c.challenge_id == used_id,
                            login_challenges.c.used_at.is_(None),
                            login_challenges.c.expires_at > now,
                        )
                    )
                    .values(used_at=now)
                )
                if used.rowcount != 1:  # used meanwhile, or the link expired
                    raise LinkError()
            await conn.execute(
                update(staff_logins)
                .where(and_(staff_logins.c.tenant == tenant, staff_logins.c.principal_id == pid))
                .values(
                    password_hash=pending["password"],
                    password_set_at=now,
                    totp_sealed=self.sealer.seal(tenant, pending["totp"]),
                    totp_last_step=step,
                    failed_attempts=0,
                    locked_until=None,
                    last_sign_in_at=now,
                )
            )
            await self.audit.record_in(conn, tenant, pid, "auth.totp.enrolled", f"principal/{pid}")
        await self.principals.end_sessions(tenant, pid)
        key, expires = await self.principals.open_session(
            tenant, pid, self._session_length(remember)
        )
        return {"key": key, "expires_at": expires.isoformat()}

    # --- signing in --------------------------------------------------------------------------

    async def begin(self, email: str, password: str) -> dict[str, str]:
        """E-mail and password. Raises SignInError. (A login gets its password and its
        authenticator together, in `enroll`: one is never there without the other.)"""
        email = normalize(email)
        async with self.db.engine.connect() as conn:
            row = (
                await self._login(conn, staff_logins.c.email == email)
                if valid_email(email)
                else None
            )
        stored = row["password_hash"] if row is not None else None
        good = await asyncio.to_thread(check_password, password, stored)
        if row is None or row["revoked_at"] is not None or row["kind"] != STAFF:
            raise SignInError()
        tenant, pid = row["tenant"], row["principal_id"]
        where = and_(staff_logins.c.tenant == tenant, staff_logins.c.principal_id == pid)
        locked = aware(row["locked_until"])
        if locked is not None and locked > utcnow():
            await self.audit.record(tenant, pid, "auth.sign_in.locked", f"principal/{pid}")
            raise SignInError()
        if not good:
            failed_at = utcnow()
            failures = staff_logins.c.failed_attempts + 1
            threshold = failures >= LOCK_AFTER
            async with self.db.engine.begin() as conn:
                # Increment the stored counter while holding the database's write
                # lock. Concurrent password checks must not overwrite one another
                # or clear a lock established since they read the account.
                result = await conn.execute(
                    update(staff_logins)
                    .where(
                        where,
                        or_(
                            staff_logins.c.locked_until.is_(None),
                            staff_logins.c.locked_until <= failed_at,
                        ),
                    )
                    .values(
                        failed_attempts=case((threshold, 0), else_=failures),
                        locked_until=case(
                            (threshold, failed_at + timedelta(minutes=LOCK_MINUTES)),
                            else_=None,
                        ),
                    )
                    .returning(staff_logins.c.locked_until)
                )
                changed = result.first()
                await self.audit.record_in(
                    conn,
                    tenant,
                    pid,
                    "auth.password.wrong" if changed is not None else "auth.sign_in.locked",
                    f"principal/{pid}",
                    details={"locked": changed is None or changed.locked_until is not None},
                )
            raise SignInError()
        async with self.db.engine.begin() as conn:
            await conn.execute(
                update(staff_logins).where(where).values(failed_attempts=0, locked_until=None)
            )
        challenge_id = await self._challenge(
            tenant, pid, SIGN_IN, timedelta(minutes=STEP_MINUTES), "-"
        )
        await self.audit.record(tenant, pid, "auth.sign_in.started", f"principal/{pid}")
        return {"challenge": challenge_id}

    async def finish(self, challenge_id: str, code: str, remember: bool) -> dict[str, str]:
        """The authenticator's code; returns the session key. Raises SignInError."""
        ch = await self._open(challenge_id, SIGN_IN)
        now = utcnow()
        if (
            ch is None
            or ch["used_at"] is not None
            or _at(ch["expires_at"]) <= now
            or ch["attempts"] >= CODE_TRIES
        ):
            raise SignInError()
        tenant, pid = ch["tenant"], ch["principal_id"]
        row = await self._row(tenant, pid)
        if row is None or row["revoked_at"] is not None or row["totp_sealed"] is None:
            raise SignInError()
        try:
            secret = unb32(self.sealer.open(tenant, row["totp_sealed"]))
        except SealError as exc:
            raise SignInError() from exc
        step = matching_step(secret, code, row["totp_last_step"])
        if step is None:
            await self._count_wrong(challenge_id, tenant, pid)
            raise SignInError()
        where = and_(staff_logins.c.tenant == tenant, staff_logins.c.principal_id == pid)
        async with self.db.engine.begin() as conn:
            used = await conn.execute(
                update(login_challenges)
                .where(
                    and_(
                        login_challenges.c.challenge_id == challenge_id,
                        login_challenges.c.used_at.is_(None),
                    )
                )
                .values(used_at=now)
            )
            # The step is taken only if no other sign-in took it first (a replayed code).
            taken = await conn.execute(
                update(staff_logins)
                .where(
                    and_(
                        where,
                        or_(
                            staff_logins.c.totp_last_step.is_(None),
                            staff_logins.c.totp_last_step < step,
                        ),
                    )
                )
                .values(totp_last_step=step, last_sign_in_at=now)
            )
            if used.rowcount != 1 or taken.rowcount != 1:
                raise SignInError()
        key, expires = await self.principals.open_session(
            tenant, pid, self._session_length(remember)
        )
        return {"key": key, "expires_at": expires.isoformat()}

    # --- one's own account, leaving, retention ---------------------------------------------

    async def of(self, tenant: str, pid: str) -> dict[str, Any] | None:
        row = await self._row(tenant, pid)
        if row is None:
            return None
        return {"email": row["email"], "active": row["totp_sealed"] is not None}

    async def purge(self, dry_run: bool = False) -> int:
        """Retention: links and steps, and ended sessions, a day after they stopped
        working (they are useless then, and only hashes and sealed values besides)."""
        cutoff = utcnow() - timedelta(days=1)
        stale = (
            (login_challenges, login_challenges.c.expires_at < cutoff),
            (
                staff_sessions,
                or_(staff_sessions.c.expires_at < cutoff, staff_sessions.c.revoked_at < cutoff),
            ),
        )
        total = 0
        async with self.db.engine.begin() as conn:
            for table, where in stale:
                if dry_run:
                    count = select(func.count()).select_from(table).where(where)
                    total += int((await conn.execute(count)).scalar() or 0)
                else:
                    total += int((await conn.execute(delete(table).where(where))).rowcount)
        return total

    async def remove(
        self, tenant: str, pid: str, actor: str, *, erase_profile: bool = False
    ) -> bool:
        """The person leaves the team: access revoked, every session ended. False for an
        unknown or non-staff id; ValueError for one's own access (nobody locks themselves
        out by mistake). With explicit erasure, remove the e-mail login and pending
        credentials too, freeing its e-mail for a fresh invitation. Stable principals,
        clinical records and audit authorship remain; an associated professional is
        disabled for new bookings. All changes commit together."""
        if pid == actor:
            raise ValueError("you cannot remove your own access")
        now = utcnow()
        async with self.db.engine.begin() as conn:
            row = (
                await conn.execute(
                    select(principals.c.kind, principals.c.revoked_at)
                    .where(and_(principals.c.tenant == tenant, principals.c.id == pid))
                    .with_for_update()
                )
            ).first()
            if row is None or row.kind != STAFF:
                return False
            login_where = and_(staff_logins.c.tenant == tenant, staff_logins.c.principal_id == pid)
            if row.revoked_at is not None:
                if not erase_profile:
                    return False
                login = await conn.execute(select(staff_logins.c.email).where(login_where))
                if login.first() is None:
                    return False
            else:
                await conn.execute(
                    update(principals)
                    .where(and_(principals.c.tenant == tenant, principals.c.id == pid))
                    .values(revoked_at=now)
                )
                await self.audit.record_in(
                    conn, tenant, actor, "principal.revoked", f"principal/{pid}"
                )
            await conn.execute(
                update(staff_sessions)
                .where(
                    and_(
                        staff_sessions.c.tenant == tenant,
                        staff_sessions.c.principal_id == pid,
                        staff_sessions.c.revoked_at.is_(None),
                    )
                )
                .values(revoked_at=now)
            )
            if erase_profile:
                from orchestrator.establishment import professionals

                await conn.execute(
                    delete(login_challenges).where(
                        and_(
                            login_challenges.c.tenant == tenant,
                            login_challenges.c.principal_id == pid,
                        )
                    )
                )
                await conn.execute(delete(staff_logins).where(login_where))
                await conn.execute(
                    update(professionals)
                    .where(and_(professionals.c.tenant == tenant, professionals.c.staff_id == pid))
                    .values(active=False)
                )
                await self.audit.record_in(
                    conn, tenant, actor, "auth.profile.erased", f"principal/{pid}"
                )
        return True
