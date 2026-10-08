"""Browser test of the console's human-review flow (audit finding A32).

A receptionist asks a clinical question and sees it held; a dentist approves it from
the Reviews tab; the receptionist's page picks up the approved answer by itself. A
rejected answer never reaches the receptionist's page. Needs Playwright and Chromium
(`uv run --with playwright python -m playwright install chromium`); skipped otherwise."""

from __future__ import annotations

import asyncio
import socket
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from orchestrator.api.app import create_app
from orchestrator.catalog import Catalog
from orchestrator.config import Settings
from orchestrator.llm import FakeLLM
from orchestrator.service import Orchestrator

sync_api = pytest.importorskip("playwright.sync_api")

SERVICE = {"X-API-Key": "test-key"}
CLINICAL = "¿Qué dosis de ibuprofeno tomo?"
DRAFT = "BORRADOR_SIN_REVISAR"
POLL_WAIT_MS = 15_000  # the console polls every 5 s


@pytest.fixture
def server(settings: Settings, catalog: Catalog) -> Iterator[str]:
    import uvicorn

    settings.tenant_packs = {"acme": "dental"}
    llm = FakeLLM(agent_replies=[DRAFT] * 10)
    app = create_app(settings, Orchestrator(settings, catalog=catalog, llm=llm))
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    deadline = time.time() + 15
    while not srv.started and time.time() < deadline:
        time.sleep(0.05)
    assert srv.started, "server did not start"
    yield f"http://127.0.0.1:{port}"
    srv.should_exit = True
    thread.join(timeout=10)


@pytest.fixture
def subprocess_loop() -> Iterator[None]:
    """Playwright starts a driver process: on Windows that needs the Proactor loop, which
    conftest swaps for the selector loop (psycopg). Restore it for this test only."""
    if sys.platform != "win32":
        yield
        return
    previous = asyncio.get_event_loop_policy()
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    try:
        yield
    finally:
        asyncio.set_event_loop_policy(previous)


def _key(base: str, name: str, *roles: str) -> str:
    resp = httpx.post(
        f"{base}/v1/admin/staff", json={"name": name, "roles": list(roles)}, headers=SERVICE
    )
    assert resp.status_code == 201, resp.text
    return str(resp.json()["key"])


def _open(browser: Any, base: str, key: str, errors: list[str], **options: Any) -> Any:
    page = browser.new_page(**options)
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    page.goto(base)
    page.fill("#api-key", key)
    page.dispatch_event("#api-key", "change")
    return page


def _ask(page: Any, question: str) -> None:
    page.fill("#question", question)
    page.press("#question", "Enter")


def test_a32_held_answer_is_shown_reviewed_and_never_leaked(
    server: str, subprocess_loop: None
) -> None:
    reception = _key(server, "maria", "reception")
    dentist = _key(server, "dr.lopez", "reviewer")
    errors: list[str] = []
    expect = sync_api.expect
    with sync_api.sync_playwright() as pw:
        try:
            browser = pw.chromium.launch()
        except sync_api.Error as exc:  # Playwright is installed but Chromium is not
            pytest.skip(f"no browser: {exc.message.splitlines()[0]}")
        asker = _open(browser, server, reception, errors)

        # 1. Held: an explicit state instead of an empty answer, and a locked composer.
        _ask(asker, CLINICAL)
        expect(asker.get_by_text("Waiting for a human review")).to_be_visible()
        expect(asker.locator("#question")).to_be_disabled()
        assert DRAFT not in asker.content()

        # 2. The dentist sees it in the Reviews tab, edits the draft and approves it.
        reviewer = _open(browser, server, dentist, errors)
        reviewer.click("#tab-reviews")
        card = reviewer.locator(".review-card").first
        expect(card).to_contain_text(CLINICAL)
        card.locator("textarea").fill("Llámenos al consultorio, por favor.")
        # The list refreshes while the reviewer is editing (another tab, a new held
        # answer, a slow response): the edit must survive. CI caught this as a race.
        with reviewer.expect_response(lambda r: r.url.endswith("/v1/reviews")):
            reviewer.click("#reviews-refresh")
        expect(reviewer.locator(".review-card textarea").first).to_have_value(
            "Llámenos al consultorio, por favor."
        )
        card.get_by_role("button", name="Approve").click()
        expect(reviewer.get_by_text("Nothing waiting for review.")).to_be_visible()

        # 3. The receptionist's page picks the approved answer up by itself.
        expect(asker.get_by_text("Llámenos al consultorio, por favor.")).to_be_visible(
            timeout=POLL_WAIT_MS
        )
        expect(asker.locator("#question")).to_be_enabled()

        # 4. A rejected answer: the draft never reaches the receptionist's page.
        asker.click("#new-thread")
        _ask(asker, CLINICAL)
        expect(asker.get_by_text("Waiting for a human review")).to_be_visible()
        reviewer.click("#reviews-refresh")
        reviewer.locator(".review-card").first.get_by_role("button", name="Reject").click()
        expect(asker.get_by_text("Not approved")).to_be_visible(timeout=POLL_WAIT_MS)
        assert DRAFT not in asker.content()
        browser.close()
    assert errors == []


# --- accessibility (audit 2026-10-08): axe-core WCAG 2.1 AA in every main state --------

AXE = Path(__file__).parent / "fixtures" / "a11y" / "axe.min.js"
AXE_SHA256 = "20c09fe157a8a34a30e241aaa1fcdade657734f08ab379ecfbeb7d45cc46e878"  # 4.14.0


def _axe(page: Any, state: str) -> list[str]:
    """WCAG 2.0/2.1 A and AA violations on the page as it is now."""
    page.add_script_tag(content=AXE.read_text(encoding="utf-8"))
    result = page.evaluate(
        "async () => await axe.run(document, {runOnly: "
        "{type: 'tag', values: ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa']}})"
    )
    return [
        f"[{state}] {v['id']} ({v['impact']}): {v['help']} -> "
        + "; ".join(n["target"][0] for n in v["nodes"][:3])
        for v in result["violations"]
    ]


def test_the_console_meets_wcag_aa_and_works_by_keyboard(
    server: str, subprocess_loop: None
) -> None:
    import hashlib

    assert hashlib.sha256(AXE.read_bytes()).hexdigest() == AXE_SHA256  # the vendored engine
    reception = _key(server, "ana", "reception")
    dentist = _key(server, "dr.ruiz", "reviewer")
    errors: list[str] = []
    expect = sync_api.expect
    violations: list[str] = []
    with sync_api.sync_playwright() as pw:
        try:
            browser = pw.chromium.launch()
        except sync_api.Error as exc:
            pytest.skip(f"no browser: {exc.message.splitlines()[0]}")
        for scheme in ("light", "dark"):
            # The console's CSP forbids inline scripts (as it should): only this test
            # page lifts it, to inject the axe engine.
            page = browser.new_page(color_scheme=scheme, bypass_csp=True)
            page.on("pageerror", lambda exc: errors.append(str(exc)))
            page.goto(server)
            violations += _axe(page, f"{scheme}: empty")
            page.fill("#api-key", reception)
            page.dispatch_event("#api-key", "change")
            expect(page.locator("#agents li").first).to_be_visible()
            violations += _axe(page, f"{scheme}: agents")
            _ask(page, "hola")
            expect(page.locator(".provenance").first).to_have_text(
                "AI-generated · not reviewed by a professional"
            )
            violations += _axe(page, f"{scheme}: answer")
            page.click("#new-thread")
            _ask(page, CLINICAL)
            expect(page.get_by_text("AI draft · pending professional review")).to_be_visible()
            violations += _axe(page, f"{scheme}: held")
            page.close()

        dark = _open(browser, server, dentist, errors, bypass_csp=True, color_scheme="dark")
        dark.click("#tab-reviews")
        expect(dark.locator(".review-card").first).to_be_visible()
        violations += _axe(dark, "dark: reviews")
        dark.close()
        reviewer = _open(browser, server, dentist, errors, bypass_csp=True)
        # Keyboard only: focus the selected tab, then arrows move between tabs.
        reviewer.focus("#tab-agents")
        reviewer.keyboard.press("ArrowRight")
        expect(reviewer.locator("#tab-knowledge")).to_be_focused()
        expect(reviewer.locator("#panel-knowledge")).to_be_visible()
        reviewer.keyboard.press("End")
        expect(reviewer.locator("#tab-patients")).to_be_focused()
        reviewer.keyboard.press("ArrowLeft")
        expect(reviewer.locator("#tab-reviews")).to_be_focused()
        expect(reviewer.locator(".review-card").first).to_be_visible()
        assert reviewer.get_attribute("#tab-agents", "tabindex") == "-1"  # one tab stop
        violations += _axe(reviewer, "reviews")
        browser.close()
    assert errors == []
    assert violations == [], "\n".join(violations)


@pytest.fixture
def down_server(settings: Settings, catalog: Catalog) -> Iterator[str]:
    """The same console with the model provider down behind its circuit breaker."""
    import uvicorn

    from orchestrator.llm import GuardedLLM
    from orchestrator.resilience import CircuitBreaker
    from tests.test_resilience import DownLLM

    llm = GuardedLLM(DownLLM(), CircuitBreaker("llm", failures=1, cooldown_s=30))
    app = create_app(settings, Orchestrator(settings, catalog=catalog, llm=llm))  # type: ignore[arg-type]
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    deadline = time.time() + 15
    while not srv.started and time.time() < deadline:
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    srv.should_exit = True
    thread.join(timeout=10)


def test_a_model_outage_reads_as_a_clear_message_and_keeps_the_text(
    down_server: str, subprocess_loop: None
) -> None:
    errors: list[str] = []
    expect = sync_api.expect
    with sync_api.sync_playwright() as pw:
        try:
            browser = pw.chromium.launch()
        except sync_api.Error as exc:
            pytest.skip(f"no browser: {exc.message.splitlines()[0]}")
        page = _open(browser, down_server, "test-key", errors)
        _ask(page, "Resume nuestra política de reembolsos")
        alert = page.get_by_role("alert")
        expect(alert).to_contain_text("temporarily unavailable")
        expect(alert).to_contain_text("Nothing was lost")
        expect(alert.get_by_role("button", name="Try again")).to_be_visible()
        expect(page.locator("#question")).to_have_value("Resume nuestra política de reembolsos")
        assert "Internal Server Error" not in page.content()
        browser.close()
    assert errors == []


def test_a_psychologist_designs_applies_and_files_without_leaving_the_console(
    server: str, subprocess_loop: None
) -> None:
    """The clinical workspace: build a test in the form, apply it and PHQ-9 to a patient,
    see score, band and alert, attach a signed consent. WCAG AA checked on each view."""
    vera = _key(server, "dra.vera", "reviewer")
    created = httpx.post(
        f"{server}/v1/crm/patients",
        json={"id": "p-ui-1", "display_name": "Paciente Sintético"},
        headers=SERVICE,
    )
    assert created.status_code == 201, created.text
    errors: list[str] = []
    violations: list[str] = []
    expect = sync_api.expect
    with sync_api.sync_playwright() as pw:
        try:
            browser = pw.chromium.launch()
        except sync_api.Error as exc:
            pytest.skip(f"no browser: {exc.message.splitlines()[0]}")
        page = _open(browser, server, vera, errors, bypass_csp=True)
        page.click("#tab-patients")
        page.click("#open-tests")
        expect(page.get_by_role("heading", name="Tests", exact=True)).to_be_focused()
        expect(page.locator("#composer")).to_be_hidden()  # the workspace replaces the chat
        violations += _axe(page, "tests view")

        # A template in one click, then a test of her own, built in the form.
        page.get_by_role("button", name="Add PHQ-9 (depression)").click()
        expect(page.get_by_text("PHQ-9 (depresión)").first).to_be_visible()
        page.fill("input[name=name]", "Bienestar semanal")
        page.fill("input[name=text-q1]", "Duermo bien")
        page.get_by_role("button", name="+ Add item").click()
        page.fill("input[name=text-q2]", "Me siento agobiado")
        page.check("input[name=reverse-q2]")
        page.fill("textarea[name=bands]", "0-2: bajo (high)\n3-6: adecuado (none)")
        page.fill("textarea[name=alerts]", "q1 <= 0: Revisar el sueño")
        page.fill("input[name=source]", "Instrumento propio")
        page.check("input[name=attestation]")
        page.get_by_role("button", name="Save test").click()
        expect(page.get_by_text("Saved “Bienestar semanal” (v1).")).to_be_visible()

        # Apply it to the patient: score, band and the alert, visibly.
        page.get_by_role("button", name="← Back to the assistant").click()
        expect(page.locator("#composer")).to_be_visible()
        page.get_by_role("button", name="Paciente Sintético p-ui-1").click()
        page.select_option("#apply-instrument", label="Bienestar semanal (v1)")
        page.get_by_label("Nunca").first.check()  # q1 = 0
        page.locator("fieldset.item").nth(1).get_by_label("Siempre").check()  # q2 = 3 -> 0
        page.get_by_role("button", name="Save result").click()
        expect(page.get_by_role("alert").filter(has_text="Revisar el sueño").first).to_be_visible()
        expect(page.locator(".result").first).to_contain_text("Score 0 · bajo")
        violations += _axe(page, "patient view")

        # A signed consent attached to the record.
        page.set_input_files(
            "input[name=file]",
            files=[
                {
                    "name": "consentimiento.pdf",
                    "mimeType": "application/pdf",
                    "buffer": b"%PDF-1.4\n% sintetico\n%%EOF\n",
                }
            ],
        )
        page.fill("input[name=label]", "Consentimiento firmado")
        page.get_by_role("button", name="Upload").click()
        expect(page.get_by_role("button", name="Download consentimiento.pdf")).to_be_visible()
        violations += _axe(page, "patient view with a document")
        browser.close()
    assert errors == []
    assert violations == [], "\n".join(violations)
