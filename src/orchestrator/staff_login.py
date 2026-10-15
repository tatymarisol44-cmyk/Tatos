"""Staff sign-in with e-mail, password and a code sent by e-mail (ADR 0024).

- **Who.** A staff principal gets an e-mail login when the administration adds the person
  in Equipo, or when the person adds an e-mail to their own account. Either way an
  invitation link lets them choose the password; nobody else ever sees it.
- **Passwords** are hashed with scrypt (N=2^13, r=8, p=10: OWASP's 8 MiB profile, sized for
  the free instance's 512 MB), a random salt each, compared in constant time. An unknown
  e-mail costs the same work as a wrong password.
- **Sign-in.** E-mail and password, then a 6-digit code by e-mail: valid 10 minutes, 5
  tries, resent at most once a minute and 5 times. The code is kept as an HMAC under the
  deployment's secret. Then a session key `ss_...` (auth.py) for 12 hours, or 30 days
  when the person asks to be remembered on that computer.
- **Locks.** 5 wrong passwords lock the login for 15 minutes.
- **Nothing tells whether an e-mail exists.** A wrong e-mail and a wrong password answer
  alike, and "I forgot my password" always answers the same.
- **Endings.** A new password, removing the person from the team, or "sign out
  everywhere" end every open session. The key login stays as break-glass access.
- Every step is audited by principal id, never with the password or the code."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import re
import secrets
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import (
    Column,
    DateTime,
    Integer,
    String,
    Table,
    and_,
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
from orchestrator.mailer import Mailer

staff_logins = Table(
    "staff_logins",
    metadata,
    Column("tenant", String(64), primary_key=True),
    Column("principal_id", String(128), primary_key=True),
    # Lower-cased. Unique across the deployment: signing in names no practice.
    Column("email", String(254), nullable=False, unique=True, index=True),
    Column("display_name", String(120), nullable=False),
    Column("password_hash", String(200), nullable=True),  # None until the invitation is used
    Column("password_set_at", DateTime(timezone=True), nullable=True),
    Column("failed_attempts", Integer, nullable=False, default=0),
    Column("locked_until", DateTime(timezone=True), nullable=True),
    Column("last_sign_in_at", DateTime(timezone=True), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("created_by", String(128), nullable=False),
)

# A code by e-mail (sign_in) or a link to choose a password (invite, reset).
login_challenges = Table(
    "login_challenges",
    metadata,
    Column("challenge_id", String(32), primary_key=True),
    Column("tenant", String(64), nullable=False),
    Column("principal_id", String(128), nullable=False, index=True),
    Column("purpose", String(16), nullable=False),
    Column("secret_hash", String(64), nullable=False, index=True),
    Column("attempts", Integer, nullable=False, default=0),
    Column("sends", Integer, nullable=False, default=1),
    Column("sent_at", DateTime(timezone=True), nullable=False),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Column("used_at", DateTime(timezone=True), nullable=True),
)

SIGN_IN, INVITE, RESET = "sign_in", "invite", "reset"
CODE_MINUTES = 10
CODE_TRIES = 5
CODE_SENDS = 5
RESEND_SECONDS = 60
INVITE_HOURS = 72
RESET_MINUTES = 30
LOCK_AFTER = 5
LOCK_MINUTES = 15
MIN_PASSWORD = 10
MAX_PASSWORD = 200
# scrypt: OWASP's 8 MiB profile (N=2^13, r=8, p=10).
_N, _R, _P = 2**13, 8, 10
_MAXMEM = 32 * 1024 * 1024
EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,189}\.[^@\s]{2,}$")
COMMON = {
    "1234567890",
    "12345678910",
    "contraseña",
    "contrasena1",
    "password123",
    "qwertyuiop",
    "consultorio",
    "consultorio1",
    "psicologia",
    "psicologia1",
    "0987654321",
}


class SignInError(Exception):
    """Wrong e-mail, password or code, or a locked login: one answer for all of them."""


class LinkError(Exception):
    """An invitation or reset link that is unknown, used or expired."""


class WeakPassword(ValueError):
    """The password does not meet the rules; the message says which one."""


class MailUnavailable(Exception):
    """The e-mail could not be sent (no provider, or the provider failed)."""


class TooSoon(Exception):
    """A code was sent less than a minute ago, or too many were sent."""


def normalize(email: str) -> str:
    return email.strip().lower()


def valid_email(email: str) -> bool:
    return len(email) <= 254 and EMAIL.fullmatch(email) is not None


def mask(email: str) -> str:
    """c***@gmail.com: enough to recognise one's own address, not to learn someone's."""
    local, _, domain = email.partition("@")
    return f"{local[:1]}***@{domain}"


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


def _at(value: Any) -> datetime:
    """A NOT NULL timestamp column, time-zone aware whatever the database returns."""
    when = aware(value)
    assert when is not None
    return when


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


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
        mailer: Mailer,
        settings: Settings,
    ) -> None:
        self.db = db
        self.audit = audit
        self.principals = principal_store
        self.mailer = mailer
        self.settings = settings
        self._code_key = hashlib.sha256(b"staff-login-code:" + settings.pseudonym_secret()).digest()

    # --- helpers ------------------------------------------------------------------------

    def _code_hash(self, challenge_id: str, code: str) -> str:
        return hmac.new(self._code_key, f"{challenge_id}:{code}".encode(), "sha256").hexdigest()

    def _link(self, token: str) -> str:
        return f"{self.settings.public_base_url.rstrip('/')}/app#clave={token}"

    async def _mail(self, to: str, subject: str, text: str) -> None:
        state, _ = await self.mailer.send(to, subject, text)
        if state in ("failed", "dry_run"):
            raise MailUnavailable(state)

    async def _login(self, conn: Any, *where: Any) -> Any:
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
        return (await conn.execute(query)).mappings().first()

    # --- the team (administration) ---------------------------------------------------------

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
                staff_logins.c.password_set_at,
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
            if r["revoked_at"] is not None:
                status = "revoked"
            elif r["email"] is None:
                status = "key_only"
            elif r["password_set_at"] is None:
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
        """A new staff member with an e-mail login, invited to choose a password. The
        answer says whether the invitation e-mail went out (`invited`)."""
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
        try:
            await self.invite(tenant, pid, actor)
            invited = True
        except MailUnavailable:
            invited = False  # the member exists; the invitation can be sent again
        member = next(m for m in await self.team(tenant) if m["id"] == pid)
        return {**member, "invited": invited}

    async def attach_own_email(
        self, tenant: str, pid: str, email: str, name: str | None = None
    ) -> None:
        """A staff member adds an e-mail login to their own account (invitation to it)."""
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
        if mine is not None and mine["password_set_at"] is not None:
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
        await self.invite(tenant, pid, pid)

    async def _attach(self, tenant: str, pid: str, actor: str, email: str, name: str) -> None:
        async with self.db.engine.begin() as conn:
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

    async def invite(self, tenant: str, pid: str, actor: str) -> None:
        """(Re)send the link to choose a password. Raises KeyError for an unknown member."""
        async with self.db.engine.connect() as conn:
            row = await self._login(
                conn, staff_logins.c.tenant == tenant, staff_logins.c.principal_id == pid
            )
        if row is None or row["revoked_at"] is not None:
            raise KeyError(pid)
        token = secrets.token_urlsafe(32)
        await self._challenge(
            tenant, pid, INVITE, timedelta(hours=INVITE_HOURS), lambda _: _sha(token)
        )
        text = (
            f"Hola {row['display_name']}:\n\n"
            "Te dieron acceso a la app del consultorio. Para entrar, crea tu contraseña con este "
            f"enlace (vence en {INVITE_HOURS} horas):\n\n{self._link(token)}\n\n"
            "Después entrarás con tu correo, tu contraseña y un código que te enviaremos aquí.\n"
            "Si no esperabas este correo, ignóralo."
        )
        await self._mail(row["email"], "Crea tu contraseña del consultorio", text)
        await self.audit.record(tenant, actor, "auth.invitation.sent", f"principal/{pid}")

    async def _challenge(
        self,
        tenant: str,
        pid: str,
        purpose: str,
        lifetime: timedelta,
        secret_of: Callable[[str], str],
    ) -> str:
        challenge_id = secrets.token_hex(16)
        secret_hash = secret_of(challenge_id)
        now = utcnow()
        async with self.db.engine.begin() as conn:
            # One open link or code of each kind per person: a new one replaces the old.
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
                    attempts=0,
                    sends=1,
                    sent_at=now,
                    expires_at=now + lifetime,
                )
            )
        return challenge_id

    # --- choosing a password -------------------------------------------------------------

    async def set_password(self, token: str, password: str) -> None:
        """Use an invitation or reset link. Raises LinkError or WeakPassword."""
        now = utcnow()
        async with self.db.engine.connect() as conn:
            link = (
                (
                    await conn.execute(
                        select(login_challenges).where(
                            and_(
                                login_challenges.c.secret_hash == _sha(token),
                                login_challenges.c.purpose.in_([INVITE, RESET]),
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
        tenant, pid = link["tenant"], link["principal_id"]
        async with self.db.engine.begin() as conn:
            used = await conn.execute(
                update(login_challenges)
                .where(
                    and_(
                        login_challenges.c.challenge_id == link["challenge_id"],
                        login_challenges.c.used_at.is_(None),
                    )
                )
                .values(used_at=now)
            )
            if used.rowcount != 1:  # used by a concurrent request
                raise LinkError()
            await conn.execute(
                update(staff_logins)
                .where(and_(staff_logins.c.tenant == tenant, staff_logins.c.principal_id == pid))
                .values(
                    password_hash=hashed, password_set_at=now, failed_attempts=0, locked_until=None
                )
            )
            await self.audit.record_in(
                conn,
                tenant,
                pid,
                "auth.password.set",
                f"principal/{pid}",
                details={"via": link["purpose"]},
            )
        await self.principals.end_sessions(tenant, pid)

    async def forgot(self, email: str) -> None:
        """Send a reset link when the e-mail belongs to an active login. Same answer always."""
        email = normalize(email)
        if not valid_email(email):
            return
        async with self.db.engine.connect() as conn:
            row = await self._login(conn, staff_logins.c.email == email)
            if row is None or row["revoked_at"] is not None:
                return
            recent = (
                await conn.execute(
                    select(login_challenges.c.sent_at).where(
                        and_(
                            login_challenges.c.tenant == row["tenant"],
                            login_challenges.c.principal_id == row["principal_id"],
                            login_challenges.c.purpose == RESET,
                            login_challenges.c.sent_at
                            > utcnow() - timedelta(seconds=RESEND_SECONDS),
                        )
                    )
                )
            ).first()
        if recent is not None:
            return
        token = secrets.token_urlsafe(32)
        tenant, pid = row["tenant"], row["principal_id"]
        await self._challenge(
            tenant, pid, RESET, timedelta(minutes=RESET_MINUTES), lambda _: _sha(token)
        )
        text = (
            f"Hola {row['display_name']}:\n\n"
            "Para elegir una contraseña nueva, abre este enlace "
            f"(vence en {RESET_MINUTES} minutos):\n\n{self._link(token)}\n\n"
            "Si no lo pediste tú, ignora este correo: tu contraseña sigue igual. Si se repite, "
            "avisa a la administración del consultorio."
        )
        try:
            await self._mail(row["email"], "Cambia tu contraseña del consultorio", text)
        except MailUnavailable:
            return  # the answer must not differ for a known e-mail
        await self.audit.record(tenant, pid, "auth.reset.sent", f"principal/{pid}")

    # --- signing in ------------------------------------------------------------------------

    async def begin(self, email: str, password: str) -> dict[str, str]:
        """E-mail and password; on success a code goes by e-mail. Raises SignInError."""
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
            failures = int(row["failed_attempts"]) + 1
            values: dict[str, Any] = {"failed_attempts": failures}
            if failures >= LOCK_AFTER:
                values = {
                    "failed_attempts": 0,
                    "locked_until": utcnow() + timedelta(minutes=LOCK_MINUTES),
                }
            async with self.db.engine.begin() as conn:
                await conn.execute(update(staff_logins).where(where).values(**values))
                await self.audit.record_in(
                    conn,
                    tenant,
                    pid,
                    "auth.password.wrong",
                    f"principal/{pid}",
                    details={"locked": "locked_until" in values},
                )
            raise SignInError()
        async with self.db.engine.begin() as conn:
            await conn.execute(
                update(staff_logins).where(where).values(failed_attempts=0, locked_until=None)
            )
        code = f"{secrets.randbelow(10**6):06d}"
        challenge_id = await self._challenge(
            tenant,
            pid,
            SIGN_IN,
            timedelta(minutes=CODE_MINUTES),
            lambda cid: self._code_hash(cid, code),
        )
        await self._send_code(row["email"], row["display_name"], code)
        await self.audit.record(tenant, pid, "auth.code.sent", f"principal/{pid}")
        return {"challenge": challenge_id, "sent_to": mask(row["email"])}

    async def _send_code(self, email: str, name: str, code: str) -> None:
        text = (
            f"Hola {name}:\n\nTu código para entrar a la app del consultorio es:\n\n    {code}\n\n"
            f"Vence en {CODE_MINUTES} minutos. No lo compartas con nadie: el consultorio nunca te "
            "lo pedirá.\nSi no intentaste entrar, cambia tu contraseña y avisa a la administración."
        )
        await self._mail(email, f"Tu código del consultorio: {code}", text)

    async def _open(self, challenge_id: str) -> Any:
        async with self.db.engine.connect() as conn:
            return (
                (
                    await conn.execute(
                        select(login_challenges).where(
                            and_(
                                login_challenges.c.challenge_id == challenge_id,
                                login_challenges.c.purpose == SIGN_IN,
                            )
                        )
                    )
                )
                .mappings()
                .first()
            )

    async def resend(self, challenge_id: str) -> None:
        """A new code for the same sign-in. Raises SignInError or TooSoon."""
        ch = await self._open(challenge_id)
        now = utcnow()
        if ch is None or ch["used_at"] is not None or _at(ch["expires_at"]) <= now:
            raise SignInError()
        if ch["sends"] >= CODE_SENDS or _at(ch["sent_at"]) > now - timedelta(
            seconds=RESEND_SECONDS
        ):
            raise TooSoon()
        async with self.db.engine.connect() as conn:
            row = await self._login(
                conn,
                staff_logins.c.tenant == ch["tenant"],
                staff_logins.c.principal_id == ch["principal_id"],
            )
        if row is None or row["revoked_at"] is not None:
            raise SignInError()
        code = f"{secrets.randbelow(10**6):06d}"
        async with self.db.engine.begin() as conn:
            await conn.execute(
                update(login_challenges)
                .where(login_challenges.c.challenge_id == challenge_id)
                .values(
                    secret_hash=self._code_hash(challenge_id, code),
                    attempts=0,
                    sends=ch["sends"] + 1,
                    sent_at=now,
                    expires_at=now + timedelta(minutes=CODE_MINUTES),
                )
            )
        await self._send_code(row["email"], row["display_name"], code)
        await self.audit.record(
            ch["tenant"], ch["principal_id"], "auth.code.sent", f"principal/{ch['principal_id']}"
        )

    async def finish(self, challenge_id: str, code: str, remember: bool) -> dict[str, str]:
        """The code from the e-mail; returns the session key. Raises SignInError."""
        ch = await self._open(challenge_id)
        now = utcnow()
        if (
            ch is None
            or ch["used_at"] is not None
            or _at(ch["expires_at"]) <= now
            or ch["attempts"] >= CODE_TRIES
        ):
            raise SignInError()
        tenant, pid = ch["tenant"], ch["principal_id"]
        good = hmac.compare_digest(self._code_hash(challenge_id, code.strip()), ch["secret_hash"])
        if not good:
            async with self.db.engine.begin() as conn:
                await conn.execute(
                    update(login_challenges)
                    .where(login_challenges.c.challenge_id == challenge_id)
                    .values(attempts=login_challenges.c.attempts + 1)
                )
                await self.audit.record_in(conn, tenant, pid, "auth.code.wrong", f"principal/{pid}")
            raise SignInError()
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
            if used.rowcount != 1:
                raise SignInError()
            await conn.execute(
                update(staff_logins)
                .where(and_(staff_logins.c.tenant == tenant, staff_logins.c.principal_id == pid))
                .values(last_sign_in_at=now)
            )
        async with self.db.engine.connect() as conn:
            row = await self._login(
                conn, staff_logins.c.tenant == tenant, staff_logins.c.principal_id == pid
            )
        if row is None or row["revoked_at"] is not None:
            raise SignInError()
        lifetime = (
            timedelta(days=self.settings.staff_session_remember_days)
            if remember
            else timedelta(hours=self.settings.staff_session_hours)
        )
        key, expires = await self.principals.open_session(tenant, pid, lifetime)
        return {"key": key, "expires_at": expires.isoformat()}

    # --- one's own account ---------------------------------------------------------------

    async def of(self, tenant: str, pid: str) -> dict[str, Any] | None:
        async with self.db.engine.connect() as conn:
            row = await self._login(
                conn, staff_logins.c.tenant == tenant, staff_logins.c.principal_id == pid
            )
        if row is None:
            return None
        return {"email": row["email"], "active": row["password_set_at"] is not None}

    async def purge(self, dry_run: bool = False) -> int:
        """Retention: codes and links, and ended sessions, a day after they stopped
        working (they are useless then, and only hashes besides)."""
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

    async def remove(self, tenant: str, pid: str, actor: str) -> bool:
        """The person leaves the team: access revoked, every session ended. False for an
        unknown or non-staff id; ValueError for one's own access (nobody locks themselves
        out by mistake)."""
        if pid == actor:
            raise ValueError("you cannot remove your own access")
        async with self.db.engine.connect() as conn:
            kind = (
                await conn.execute(
                    select(principals.c.kind).where(
                        and_(principals.c.tenant == tenant, principals.c.id == pid)
                    )
                )
            ).scalar()
        if kind != STAFF:
            return False
        done = await self.principals.revoke(tenant, pid, actor)
        if done:
            await self.principals.end_sessions(tenant, pid)
        return done
