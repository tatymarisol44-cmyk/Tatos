"""Staff sign-in with e-mail, password and an authenticator app (ADR 0024). Synthetic data;
the tests compute the app's codes themselves, with a clock they move."""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from orchestrator import staff_login
from orchestrator.api.app import create_app
from orchestrator.catalog import Catalog
from orchestrator.config import Settings
from orchestrator.llm import FakeLLM
from orchestrator.service import Orchestrator
from orchestrator.staff_login import (
    check_password,
    hash_password,
    login_challenges,
    password_problem,
    qr_rows,
    staff_logins,
    totp_at,
    unb32,
)

ADMIN = {"X-API-Key": "test-key"}
OTHER = {"X-API-Key": "other-key"}  # another practice's administration
GOOD = "Cuaderno-Azul-2026"
WRONG = "wrong e-mail, password or code"
ANA = "ana.perez@example.test"


@dataclass
class Clock:
    now: float = 1_800_000_000.0

    def tick(self) -> None:
        self.now += 30  # the app shows a new code every 30 seconds


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    c = Clock()
    monkeypatch.setattr(staff_login, "_time", lambda: c.now)
    return c


@pytest.fixture
def client(settings: Settings, catalog: Catalog, clock: Clock) -> Iterator[TestClient]:
    settings.auth_rate_per_minute = 1000
    settings.practice_display_name = "Consultorio Demo"
    with TestClient(
        create_app(settings, Orchestrator(settings, catalog=catalog, llm=FakeLLM()))
    ) as c:
        yield c


def code(secret: bytes, clock: Clock) -> str:
    """What the person's app shows right now."""
    return totp_at(secret, int(clock.now // 30))


def token_of(link: str) -> str:
    found = re.search(r"/app#clave=([\w-]+)$", link)
    assert found, link
    return found.group(1)


def add(client: TestClient, email: str = ANA, roles: tuple[str, ...] = ("reception",)) -> Any:
    r = client.post(
        "/v1/admin/team",
        json={"name": "Ana Pérez", "email": email, "roles": list(roles)},
        headers=ADMIN,
    )
    assert r.status_code == 201, r.text
    return r.json()


def enroll(
    client: TestClient, clock: Clock, link: str, password: str = GOOD
) -> tuple[bytes, dict[str, str]]:
    """Use a personal link: password, the app's first code; signed in."""
    started = client.post("/v1/auth/link", json={"token": token_of(link), "password": password})
    assert started.status_code == 200, started.text
    secret = unb32(started.json()["secret"])
    body = {"challenge": started.json()["challenge"], "code": code(secret, clock)}
    done = client.post("/v1/auth/enroll", json=body)
    assert done.status_code == 200, done.text
    return secret, {"X-API-Key": done.json()["key"]}


def active(
    client: TestClient, clock: Clock, email: str = ANA, roles: tuple[str, ...] = ("reception",)
) -> bytes:
    secret, _ = enroll(client, clock, add(client, email, roles)["link"])
    return secret


def signed_in(
    client: TestClient,
    clock: Clock,
    secret: bytes,
    email: str = ANA,
    password: str = GOOD,
    remember: bool = False,
) -> dict[str, str]:
    clock.tick()  # a code works once: sign in with the next one
    started = client.post("/v1/auth/sign-in", json={"email": email, "password": password})
    assert started.status_code == 200, started.text
    body = {
        "challenge": started.json()["challenge"],
        "code": code(secret, clock),
        "remember": remember,
    }
    done = client.post("/v1/auth/code", json=body)
    assert done.status_code == 200, done.text
    return {"X-API-Key": done.json()["key"]}


def test_an_invited_member_sets_a_password_and_an_authenticator(
    client: TestClient, clock: Clock
) -> None:
    m = add(client, "Ana.Perez@Example.test")
    assert m["id"] == "ana.perez" and m["status"] == "invited" and m["email"] == ANA
    assert m["link"].startswith("http://localhost:8000/app#clave=") and m["link_expires_at"]
    token = token_of(m["link"])

    weak = client.post("/v1/auth/link", json={"token": token, "password": "corta"})
    assert weak.status_code == 422 and "al menos 10" in weak.json()["detail"]
    mine = client.post("/v1/auth/link", json={"token": token, "password": "ana.perez-2026x"})
    assert mine.status_code == 422 and "correo" in mine.json()["detail"]

    started = client.post("/v1/auth/link", json={"token": token, "password": GOOD})
    assert started.status_code == 200
    step = started.json()
    assert re.fullmatch(r"[A-Z2-7]{32}", step["secret"])
    assert step["uri"].startswith("otpauth://totp/Consultorio%20Demo:ana.perez%40example.test?")
    assert f"secret={step['secret']}" in step["uri"] and "issuer=Consultorio%20Demo" in step["uri"]
    rows = step["qr"]
    assert len(rows) >= 29 and all(len(r) == len(rows) and set(r) <= {"0", "1"} for r in rows)
    secret = unb32(step["secret"])

    # Nothing is saved until the app's first code confirms it.
    early = client.post("/v1/auth/sign-in", json={"email": ANA, "password": GOOD})
    assert early.status_code == 401
    bad = "000000" if code(secret, clock) != "000000" else "111111"
    wrong = client.post("/v1/auth/enroll", json={"challenge": step["challenge"], "code": bad})
    assert wrong.status_code == 401
    first = code(secret, clock)
    done = client.post("/v1/auth/enroll", json={"challenge": step["challenge"], "code": first})
    assert done.status_code == 200
    key = done.json()["key"]
    assert key.startswith("ss_")
    again = client.post("/v1/auth/link", json={"token": token, "password": GOOD})
    assert again.status_code == 410  # a link works once

    session = client.get("/v1/session", headers={"X-API-Key": key}).json()
    assert session["id"] == "ana.perez" and session["roles"] == ["reception"]
    assert session["email_login"] == {"email": ANA, "active": True}
    team = {t["id"]: t["status"] for t in client.get("/v1/admin/team", headers=ADMIN).json()}
    assert team["ana.perez"] == "active"
    assert client.post("/v1/auth/sign-out", headers={"X-API-Key": key}).status_code == 204
    assert client.get("/v1/session", headers={"X-API-Key": key}).status_code == 401

    # A wrong password and an unknown e-mail answer exactly alike.
    pw = client.post("/v1/auth/sign-in", json={"email": ANA, "password": "otra-cosa-123"})
    unknown = client.post(
        "/v1/auth/sign-in", json={"email": "nadie@example.test", "password": GOOD}
    )
    assert pw.status_code == unknown.status_code == 401
    assert pw.json() == unknown.json() == {"detail": WRONG}

    # The code of a step already used is refused (a replay), the next one works.
    started = client.post(
        "/v1/auth/sign-in", json={"email": "ANA.PEREZ@example.test ", "password": GOOD}
    )
    assert started.status_code == 200 and set(started.json()) == {"challenge"}
    challenge = started.json()["challenge"]
    replay = client.post("/v1/auth/code", json={"challenge": challenge, "code": first})
    assert replay.status_code == 401
    clock.tick()
    ok = client.post("/v1/auth/code", json={"challenge": challenge, "code": code(secret, clock)})
    assert ok.status_code == 200
    reused = client.post(
        "/v1/auth/code", json={"challenge": challenge, "code": code(secret, clock)}
    )
    assert reused.status_code == 401  # a sign-in is used once

    # Nothing secret is written to the audit trail.
    audit = client.get("/v1/audit?limit=200", headers=ADMIN).text
    for secret_text in (GOOD, step["secret"], first, key, ok.json()["key"]):
        assert secret_text not in audit


def test_the_authenticator_secret_and_the_pending_password_are_sealed(
    client: TestClient, clock: Clock
) -> None:
    m = add(client)
    started = client.post("/v1/auth/link", json={"token": token_of(m["link"]), "password": GOOD})
    secret_b32 = started.json()["secret"]
    orch = client.app.state.orchestrator  # type: ignore[attr-defined]

    async def stored() -> str:
        async with orch.db.engine.connect() as conn:
            rows = [
                *await conn.execute(select(login_challenges)),
                *await conn.execute(select(staff_logins)),
            ]
        return " ".join(str(tuple(r)) for r in rows)

    pending = client.portal.call(stored)  # type: ignore[union-attr]
    assert secret_b32 not in pending and GOOD not in pending and "scrypt$" not in pending
    secret = unb32(secret_b32)
    body = {"challenge": started.json()["challenge"], "code": code(secret, clock)}
    assert client.post("/v1/auth/enroll", json=body).status_code == 200
    saved = client.portal.call(stored)  # type: ignore[union-attr]
    assert secret_b32 not in saved and GOOD not in saved and "scrypt$8192$8$10$" in saved


def test_five_wrong_passwords_lock_the_login(client: TestClient, clock: Clock) -> None:
    active(client, clock)
    for _ in range(5):
        r = client.post("/v1/auth/sign-in", json={"email": ANA, "password": "no-es-esta-1"})
        assert r.status_code == 401
    locked = client.post("/v1/auth/sign-in", json={"email": ANA, "password": GOOD})
    assert locked.status_code == 401 and locked.json() == {"detail": WRONG}


def test_a_sign_in_allows_five_wrong_codes(client: TestClient, clock: Clock) -> None:
    secret = active(client, clock)
    clock.tick()
    started = client.post("/v1/auth/sign-in", json={"email": ANA, "password": GOOD})
    challenge = started.json()["challenge"]
    right = code(secret, clock)
    for attempt in [
        c for c in ("000000", "111111", "222222", "333333", "444444", "555555") if c != right
    ][:5]:
        assert (
            client.post("/v1/auth/code", json={"challenge": challenge, "code": attempt}).status_code
            == 401
        )
    assert (
        client.post("/v1/auth/code", json={"challenge": challenge, "code": right}).status_code
        == 401
    )


def test_a_new_link_replaces_password_and_app_and_ends_sessions(
    client: TestClient, clock: Clock
) -> None:
    old_secret = active(client, clock)
    old_session = signed_in(client, clock, old_secret)
    link = client.post("/v1/admin/team/ana.perez/link", headers=ADMIN)
    assert link.status_code == 200 and "#clave=" in link.json()["link"]
    assert client.get("/v1/session", headers=old_session).status_code == 200  # not used yet
    new = "Otro-Cuaderno-2027"
    new_secret, new_session = enroll(client, clock, link.json()["link"], new)
    assert client.get("/v1/session", headers=old_session).status_code == 401
    assert client.get("/v1/session", headers=new_session).status_code == 200
    assert client.post("/v1/auth/sign-in", json={"email": ANA, "password": GOOD}).status_code == 401
    clock.tick()
    started = client.post("/v1/auth/sign-in", json={"email": ANA, "password": new}).json()
    stale = {"challenge": started["challenge"], "code": code(old_secret, clock)}
    if code(old_secret, clock) != code(new_secret, clock):
        assert client.post("/v1/auth/code", json=stale).status_code == 401  # the lost phone
    signed_in(client, clock, new_secret, password=new)


def test_removing_a_member_ends_their_sessions(client: TestClient, clock: Clock) -> None:
    secret = active(client, clock)
    session = signed_in(client, clock, secret)
    assert client.delete("/v1/admin/team/ana.perez", headers=OTHER).status_code == 404  # not theirs
    assert client.post("/v1/admin/team/ana.perez/link", headers=OTHER).status_code == 404
    assert client.get("/v1/session", headers=session).status_code == 200
    assert client.delete("/v1/admin/team/ana.perez", headers=ADMIN).status_code == 204
    assert client.get("/v1/session", headers=session).status_code == 401
    assert client.post("/v1/auth/sign-in", json={"email": ANA, "password": GOOD}).status_code == 401
    assert client.post("/v1/admin/team/ana.perez/link", headers=ADMIN).status_code == 404
    team = {t["id"]: t["status"] for t in client.get("/v1/admin/team", headers=ADMIN).json()}
    assert team["ana.perez"] == "revoked"


def test_sign_out_everywhere_and_expiry_end_a_session(client: TestClient, clock: Clock) -> None:
    secret = active(client, clock)
    session = signed_in(client, clock, secret)
    assert client.post("/v1/admin/principals/ana.perez/sign-out", headers=ADMIN).status_code == 204
    assert client.get("/v1/session", headers=session).status_code == 401
    orch = client.app.state.orchestrator  # type: ignore[attr-defined]
    key, _ = client.portal.call(  # type: ignore[union-attr]
        orch.principals.open_session, "acme", "ana.perez", timedelta(seconds=-1)
    )
    assert client.get("/v1/session", headers={"X-API-Key": key}).status_code == 401  # expired
    fresh, _ = client.portal.call(  # type: ignore[union-attr]
        orch.principals.open_session, "acme", "ana.perez", timedelta(hours=1)
    )
    assert client.get("/v1/session", headers={"X-API-Key": fresh}).status_code == 200


def test_an_administrator_cannot_remove_their_own_access(client: TestClient, clock: Clock) -> None:
    secret = active(client, clock, "jefa@example.test", roles=("admin",))
    jefa = signed_in(client, clock, secret, email="jefa@example.test")
    assert client.delete("/v1/admin/team/jefa", headers=jefa).status_code == 409
    assert client.get("/v1/session", headers=jefa).status_code == 200


def test_a_key_account_adds_its_own_e_mail_login(client: TestClient, clock: Clock) -> None:
    staff = client.post(
        "/v1/admin/staff", json={"name": "carla.l", "roles": ["owner", "reviewer"]}, headers=ADMIN
    ).json()
    carla = {"X-API-Key": staff["key"]}
    assert (
        client.post("/v1/session/email", json={"email": "no-es-correo"}, headers=carla).status_code
        == 422
    )
    made = client.post("/v1/session/email", json={"email": "carla@example.test"}, headers=carla)
    assert made.status_code == 200 and "#clave=" in made.json()["link"]
    login = client.get("/v1/session", headers=carla).json()["email_login"]
    assert login == {"email": "carla@example.test", "active": False}
    _, by_email = enroll(client, clock, made.json()["link"])
    session = client.get("/v1/session", headers=by_email).json()
    assert session["id"] == "carla.l" and session["clinician"] is True
    assert client.get("/v1/session", headers=carla).status_code == 200  # the key still works
    # An account that already signs in with e-mail cannot be re-pointed by a stolen session.
    again = client.post("/v1/session/email", json={"email": "x@example.test"}, headers=by_email)
    assert again.status_code == 409
    # A service key is not a person.
    service = client.post("/v1/session/email", json={"email": "svc@example.test"}, headers=ADMIN)
    assert service.status_code == 403


def test_only_the_administration_manages_the_team(client: TestClient, clock: Clock) -> None:
    ana = signed_in(client, clock, active(client, clock))
    assert client.get("/v1/admin/team", headers=ana).status_code == 403
    body = {"name": "Intrusa", "email": "intrusa@example.test", "roles": ["admin"]}
    assert client.post("/v1/admin/team", json=body, headers=ana).status_code == 403
    assert client.post("/v1/admin/team/ana.perez/link", headers=ana).status_code == 403
    taken = {"name": "Otra", "email": ANA, "roles": ["reception"]}
    assert client.post("/v1/admin/team", json=taken, headers=ADMIN).status_code == 409
    assert (
        client.post("/v1/admin/team", json={**taken, "email": "mal"}, headers=ADMIN).status_code
        == 422
    )
    assert client.post("/v1/admin/team/nadie/link", headers=ADMIN).status_code == 404


def test_sign_in_attempts_are_limited_per_address(settings: Settings, catalog: Catalog) -> None:
    settings.auth_rate_per_minute = 3
    with TestClient(
        create_app(settings, Orchestrator(settings, catalog=catalog, llm=FakeLLM()))
    ) as c:
        codes = [
            c.post(
                "/v1/auth/sign-in", json={"email": "x@example.test", "password": "y"}
            ).status_code
            for _ in range(4)
        ]
    assert codes == [401, 401, 401, 429]


def test_retention_removes_dead_links_and_sessions(client: TestClient, clock: Clock) -> None:
    signed_in(client, clock, active(client, clock))
    orch = client.app.state.orchestrator  # type: ignore[attr-defined]
    client.portal.call(  # type: ignore[union-attr]
        orch.principals.open_session, "acme", "ana.perez", timedelta(days=-2)
    )
    counted = client.portal.call(orch.retention, timedelta(days=365), True)  # type: ignore[union-attr]
    assert counted["sign_in_leftovers"] == 1  # the long-expired session; fresh rows stay
    client.portal.call(orch.retention, timedelta(days=365))  # type: ignore[union-attr]
    again = client.portal.call(orch.retention, timedelta(days=365), True)  # type: ignore[union-attr]
    assert again["sign_in_leftovers"] == 0


def test_codes_match_rfc_6238() -> None:
    """The RFC's own test values (SHA-1): what every authenticator app computes."""
    secret = b"12345678901234567890"
    assert totp_at(secret, 59 // 30, digits=8) == "94287082"
    assert totp_at(secret, 1111111109 // 30, digits=8) == "07081804"
    assert totp_at(secret, 1234567890 // 30, digits=8) == "89005924"
    assert totp_at(secret, 59 // 30) == "287082"


def test_passwords_are_salted_hashes_checked_in_constant_time() -> None:
    one, two = hash_password(GOOD), hash_password(GOOD)
    assert one != two and one.startswith("scrypt$8192$8$10$") and GOOD not in one
    assert check_password(GOOD, one) and not check_password("otra", one)
    assert not check_password(GOOD, None) and not check_password(GOOD, "md5$x")
    assert password_problem("1234567890", "a@b.test") is not None
    assert password_problem("aaaaaaaaaaaa", "a@b.test") is not None
    assert password_problem(GOOD, "a@b.test") is None
    assert len(qr_rows("otpauth://totp/x?secret=ABC")) >= 29
