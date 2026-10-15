"""Staff sign-in with e-mail, password and an e-mailed code (ADR 0024). Synthetic data; the
e-mails land in the mailer's outbox (no SMTP in tests)."""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from orchestrator.api.app import create_app
from orchestrator.catalog import Catalog
from orchestrator.config import Settings
from orchestrator.llm import FakeLLM
from orchestrator.mailer import Mailer
from orchestrator.service import Orchestrator
from orchestrator.staff_login import check_password, hash_password, mask, password_problem

ADMIN = {"X-API-Key": "test-key"}
OTHER = {"X-API-Key": "other-key"}  # another practice's administration
GOOD = "Cuaderno-Azul-2026"
WRONG = "wrong e-mail, password or code"


@pytest.fixture
def client(settings: Settings, catalog: Catalog) -> Iterator[TestClient]:
    settings.auth_rate_per_minute = 1000
    with TestClient(
        create_app(settings, Orchestrator(settings, catalog=catalog, llm=FakeLLM()))
    ) as c:
        yield c


def outbox(client: TestClient) -> list:  # type: ignore[type-arg]
    return client.app.state.orchestrator.mailer.outbox  # type: ignore[attr-defined,no-any-return]


def last_link(client: TestClient, to: str) -> str:
    mail = [m for m in outbox(client) if m.to == to][-1]
    found = re.search(r"/app#clave=([\w-]+)", mail.text)
    assert found, mail.text
    return found.group(1)


def last_code(client: TestClient, to: str) -> str:
    mail = [m for m in outbox(client) if m.to == to][-1]
    found = re.search(r"\b(\d{6})\b", mail.text)
    assert found and found.group(1) in mail.subject
    return found.group(1)


def member(client: TestClient, email: str = "ana.perez@example.test", roles=("reception",)) -> dict:  # type: ignore[no-untyped-def,type-arg]
    r = client.post(
        "/v1/admin/team",
        json={"name": "Ana Pérez", "email": email, "roles": list(roles)},
        headers=ADMIN,
    )
    assert r.status_code == 201, r.text
    return r.json()  # type: ignore[no-any-return]


def signed_in(
    client: TestClient, email: str, password: str = GOOD, remember: bool = False
) -> dict[str, str]:
    started = client.post("/v1/auth/sign-in", json={"email": email, "password": password})
    assert started.status_code == 200, started.text
    body = {
        "challenge": started.json()["challenge"],
        "code": last_code(client, email),
        "remember": remember,
    }
    done = client.post("/v1/auth/code", json=body)
    assert done.status_code == 200, done.text
    return {"X-API-Key": done.json()["key"]}


def active(client: TestClient, email: str = "ana.perez@example.test", roles=("reception",)) -> dict:  # type: ignore[no-untyped-def,type-arg]
    m = member(client, email, roles)
    token = last_link(client, email)
    assert (
        client.post("/v1/auth/password", json={"token": token, "password": GOOD}).status_code == 204
    )
    return m


def test_an_invited_member_chooses_a_password_and_signs_in_with_a_code(client: TestClient) -> None:
    m = member(client, "Ana.Perez@Example.test")
    assert m["id"] == "ana.perez" and m["status"] == "invited" and m["invited"] is True
    assert m["email"] == "ana.perez@example.test"
    token = last_link(client, "ana.perez@example.test")

    weak = client.post("/v1/auth/password", json={"token": token, "password": "corta"})
    assert weak.status_code == 422 and "al menos 10" in weak.json()["detail"]
    mine = client.post("/v1/auth/password", json={"token": token, "password": "ana.perez-2026x"})
    assert mine.status_code == 422 and "correo" in mine.json()["detail"]
    assert (
        client.post("/v1/auth/password", json={"token": token, "password": GOOD}).status_code == 204
    )
    again = client.post("/v1/auth/password", json={"token": token, "password": GOOD})
    assert again.status_code == 410  # a link works once

    team = client.get("/v1/admin/team", headers=ADMIN).json()
    assert {t["id"]: t["status"] for t in team}["ana.perez"] == "active"

    # A wrong password and an unknown e-mail answer exactly alike.
    wrong = client.post(
        "/v1/auth/sign-in", json={"email": "ana.perez@example.test", "password": "otra-cosa-123"}
    )
    unknown = client.post(
        "/v1/auth/sign-in", json={"email": "nadie@example.test", "password": GOOD}
    )
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json() == {"detail": WRONG}

    started = client.post(
        "/v1/auth/sign-in", json={"email": "ANA.PEREZ@example.test ", "password": GOOD}
    )
    assert started.status_code == 200
    assert started.json()["sent_to"] == "a***@example.test"
    challenge = started.json()["challenge"]
    code = last_code(client, "ana.perez@example.test")
    bad = "000000" if code != "000000" else "111111"
    assert (
        client.post("/v1/auth/code", json={"challenge": challenge, "code": bad}).status_code == 401
    )
    done = client.post("/v1/auth/code", json={"challenge": challenge, "code": code})
    assert done.status_code == 200
    key = done.json()["key"]
    assert key.startswith("ss_")
    reused = client.post("/v1/auth/code", json={"challenge": challenge, "code": code})
    assert reused.status_code == 401  # a code works once

    session = client.get("/v1/session", headers={"X-API-Key": key}).json()
    assert session["id"] == "ana.perez" and session["roles"] == ["reception"]
    assert session["email_login"] == {"email": "ana.perez@example.test", "active": True}
    assert client.get("/v1/crm/patients", headers={"X-API-Key": key}).status_code == 200

    # Nothing secret is kept in the clear or written to the audit trail.
    audit = client.get("/v1/audit?limit=200", headers=ADMIN).text
    assert GOOD not in audit and code not in audit and key not in audit

    assert client.post("/v1/auth/sign-out", headers={"X-API-Key": key}).status_code == 204
    assert client.get("/v1/session", headers={"X-API-Key": key}).status_code == 401


def test_five_wrong_passwords_lock_the_login(client: TestClient) -> None:
    active(client)
    for _ in range(5):
        r = client.post(
            "/v1/auth/sign-in", json={"email": "ana.perez@example.test", "password": "no-es-esta-1"}
        )
        assert r.status_code == 401
    locked = client.post(
        "/v1/auth/sign-in", json={"email": "ana.perez@example.test", "password": GOOD}
    )
    assert locked.status_code == 401 and locked.json() == {"detail": WRONG}


def test_a_code_has_five_tries_and_resending_waits_a_minute(client: TestClient) -> None:
    active(client)
    started = client.post(
        "/v1/auth/sign-in", json={"email": "ana.perez@example.test", "password": GOOD}
    )
    challenge = started.json()["challenge"]
    code = last_code(client, "ana.perez@example.test")
    wrong = [c for c in ("000000", "111111", "222222", "333333", "444444", "555555") if c != code][
        :5
    ]
    for attempt in wrong:
        assert (
            client.post("/v1/auth/code", json={"challenge": challenge, "code": attempt}).status_code
            == 401
        )
    assert (
        client.post("/v1/auth/code", json={"challenge": challenge, "code": code}).status_code == 401
    )
    assert client.post("/v1/auth/resend", json={"challenge": challenge}).status_code == 429


def test_forgot_password_answers_the_same_and_a_new_password_ends_sessions(
    client: TestClient,
) -> None:
    active(client)
    old = signed_in(client, "ana.perez@example.test")
    known = client.post("/v1/auth/forgot", json={"email": "ana.perez@example.test"})
    unknown = client.post("/v1/auth/forgot", json={"email": "nadie@example.test"})
    assert known.status_code == unknown.status_code == 202 and known.json() == unknown.json()
    token = last_link(client, "ana.perez@example.test")
    new = "Otro-Cuaderno-2027"
    assert (
        client.post("/v1/auth/password", json={"token": token, "password": new}).status_code == 204
    )
    assert client.get("/v1/session", headers=old).status_code == 401
    assert (
        client.post(
            "/v1/auth/sign-in", json={"email": "ana.perez@example.test", "password": GOOD}
        ).status_code
        == 401
    )
    signed_in(client, "ana.perez@example.test", new)


def test_removing_a_member_ends_their_sessions(client: TestClient) -> None:
    active(client)
    session = signed_in(client, "ana.perez@example.test")
    assert client.delete("/v1/admin/team/ana.perez", headers=OTHER).status_code == 404  # not theirs
    assert client.get("/v1/session", headers=session).status_code == 200
    assert client.delete("/v1/admin/team/ana.perez", headers=ADMIN).status_code == 204
    assert client.get("/v1/session", headers=session).status_code == 401
    assert (
        client.post(
            "/v1/auth/sign-in", json={"email": "ana.perez@example.test", "password": GOOD}
        ).status_code
        == 401
    )
    team = {t["id"]: t["status"] for t in client.get("/v1/admin/team", headers=ADMIN).json()}
    assert team["ana.perez"] == "revoked"


def test_sign_out_everywhere_and_expiry_end_a_session(client: TestClient) -> None:
    active(client)
    session = signed_in(client, "ana.perez@example.test")
    assert client.post("/v1/admin/principals/ana.perez/sign-out", headers=ADMIN).status_code == 204
    assert client.get("/v1/session", headers=session).status_code == 401
    orch = client.app.state.orchestrator  # type: ignore[attr-defined]
    key, _ = client.portal.call(
        orch.principals.open_session, "acme", "ana.perez", timedelta(seconds=-1)
    )  # type: ignore[union-attr]
    assert client.get("/v1/session", headers={"X-API-Key": key}).status_code == 401  # expired
    fresh, _ = client.portal.call(  # type: ignore[union-attr]
        orch.principals.open_session, "acme", "ana.perez", timedelta(hours=1)
    )
    assert client.get("/v1/session", headers={"X-API-Key": fresh}).status_code == 200


def test_an_administrator_cannot_remove_their_own_access(client: TestClient) -> None:
    active(client, "jefa@example.test", roles=("admin",))
    jefa = signed_in(client, "jefa@example.test")
    assert client.delete("/v1/admin/team/jefa", headers=jefa).status_code == 409
    assert client.get("/v1/session", headers=jefa).status_code == 200


def test_a_key_account_adds_its_own_email(client: TestClient) -> None:
    staff = client.post(
        "/v1/admin/staff", json={"name": "carla.l", "roles": ["owner", "reviewer"]}, headers=ADMIN
    ).json()
    carla = {"X-API-Key": staff["key"]}
    assert (
        client.post("/v1/session/email", json={"email": "no-es-correo"}, headers=carla).status_code
        == 422
    )
    assert (
        client.post(
            "/v1/session/email", json={"email": "carla@example.test"}, headers=carla
        ).status_code
        == 202
    )
    assert client.get("/v1/session", headers=carla).json()["email_login"] == {
        "email": "carla@example.test",
        "active": False,
    }
    token = last_link(client, "carla@example.test")
    assert (
        client.post("/v1/auth/password", json={"token": token, "password": GOOD}).status_code == 204
    )
    by_email = signed_in(client, "carla@example.test", remember=True)
    session = client.get("/v1/session", headers=by_email).json()
    assert session["id"] == "carla.l" and session["clinician"] is True
    assert client.get("/v1/session", headers=carla).status_code == 200  # the key still works
    # An account that already signs in with e-mail cannot be re-pointed by a stolen session.
    assert (
        client.post(
            "/v1/session/email", json={"email": "x@example.test"}, headers=by_email
        ).status_code
        == 409
    )
    # A service key is not a person.
    assert (
        client.post(
            "/v1/session/email", json={"email": "svc@example.test"}, headers=ADMIN
        ).status_code
        == 403
    )


def test_only_the_administration_manages_the_team(client: TestClient) -> None:
    active(client)
    ana = signed_in(client, "ana.perez@example.test")
    assert client.get("/v1/admin/team", headers=ana).status_code == 403
    body = {"name": "Intrusa", "email": "intrusa@example.test", "roles": ["admin"]}
    assert client.post("/v1/admin/team", json=body, headers=ana).status_code == 403
    taken = {"name": "Otra", "email": "ana.perez@example.test", "roles": ["reception"]}
    assert client.post("/v1/admin/team", json=taken, headers=ADMIN).status_code == 409
    assert (
        client.post("/v1/admin/team", json={**taken, "email": "mal"}, headers=ADMIN).status_code
        == 422
    )
    assert client.post("/v1/admin/team/ana.perez/invite", headers=ADMIN).status_code == 204
    assert client.post("/v1/admin/team/nadie/invite", headers=ADMIN).status_code == 404


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


def test_options_say_whether_e_mail_can_be_sent(client: TestClient) -> None:
    assert client.get("/v1/auth/options").json() == {"email": True}
    assert Mailer(Settings(app_env="prod")).available is False
    assert Mailer(Settings(app_env="prod", smtp_host="smtp.example.test")).available is True


def test_passwords_are_salted_hashes_checked_in_constant_time() -> None:
    one, two = hash_password(GOOD), hash_password(GOOD)
    assert one != two and one.startswith("scrypt$8192$8$10$") and GOOD not in one
    assert check_password(GOOD, one) and not check_password("otra", one)
    assert not check_password(GOOD, None) and not check_password(GOOD, "md5$x")
    assert password_problem("1234567890", "a@b.test") is not None
    assert password_problem("aaaaaaaaaaaa", "a@b.test") is not None
    assert password_problem(GOOD, "a@b.test") is None
    assert mask("carla@gmail.com") == "c***@gmail.com"


def test_the_dev_outbox_writes_files(settings: Settings, tmp_path) -> None:  # type: ignore[no-untyped-def]
    import anyio

    settings.dev_outbox_dir = str(tmp_path)
    state, _ = anyio.run(Mailer(settings).send, "a@b.test", "Asunto", "Hola")
    assert state == "kept"
    files = list(tmp_path.glob("*.txt"))
    assert len(files) == 1 and "Asunto" in files[0].read_text(encoding="utf-8")


def test_retention_removes_dead_codes_and_sessions(client: TestClient) -> None:
    active(client)
    signed_in(client, "ana.perez@example.test")
    orch = client.app.state.orchestrator  # type: ignore[attr-defined]
    client.portal.call(  # type: ignore[union-attr]
        orch.principals.open_session, "acme", "ana.perez", timedelta(days=-2)
    )
    counted = client.portal.call(orch.retention, timedelta(days=365), True)  # type: ignore[union-attr]
    assert counted["sign_in_leftovers"] == 1  # the long-expired session; fresh rows stay
    client.portal.call(orch.retention, timedelta(days=365))  # type: ignore[union-attr]
    again = client.portal.call(orch.retention, timedelta(days=365), True)  # type: ignore[union-attr]
    assert again["sign_in_leftovers"] == 0
