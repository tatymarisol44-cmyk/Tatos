"""`agency demo`: a synthetic psychology practice that walks every flow through the real
HTTP API, so a fresh deployment (or a Codespace) can be shown end to end in minutes.

Everything is synthetic: invented names, 555 phone numbers, example.test e-mails. Run it
against a demo tenant only; it never touches another tenant. The client is injectable,
so the test suite runs the very same script against the app in memory (tests/test_demo.py).

What it does, in order (each step is a line of the report):
 1. the team: personal keys per role and two professionals with their own packs;
 2. 20 patients with visits, plans and consents (some opted in to marketing);
 3. the clinical record: session notes, a psychotherapy note (author only), a custom
    instrument designed by the psychologist, PHQ-9/GAD-7 results (one raises an alert),
    a signed-consent PDF attached;
 4. the company knowledge base (FAQ, prices, cancellation policy);
 5. the assistant: an everyday question, and a clinical one held for review, edited and
    approved by the psychologist;
 6. the patient's own view: access link, appointments, a chat about their own records;
 7. an engagement campaign with a holdout group, approved and sent (live on Telegram
    when a bot token and a chat id are given, otherwise rehearsed);
 8. social media: accounts on every network, a generated infographic approved and
    published (for real where credentials exist, otherwise a dry run that shows the
    exact request);
 9. a crisis message on WhatsApp opens an alert for the on-call professional;
10. data-subject rights (export, erasure) and the audit chain verified.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import random
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

FIRST = [
    "Ana",
    "Luis",
    "María",
    "Jorge",
    "Paola",
    "Andrés",
    "Gabriela",
    "Diego",
    "Valeria",
    "Santiago",
    "Camila",
    "Mateo",
    "Daniela",
    "Sebastián",
    "Lucía",
    "Martín",
    "Sofía",
    "Nicolás",
    "Isabela",
    "Tomás",
]
LAST = [
    "Quishpe",
    "Andrango",
    "Guamán",
    "Cevallos",
    "Paredes",
    "Zambrano",
    "Villacís",
    "Toapanta",
    "Mora",
    "Salazar",
    "Chiriboga",
    "Benítez",
]
WA_NUMBER_ID = "100000000000001"  # synthetic WhatsApp phone-number id
CRISIS_TEXT = "Ya no quiero vivir, pienso en hacerme daño"
TEMPLATE = (
    "Hola {first_name}, en el consultorio preparamos una guía breve para cuidar el sueño "
    "y manejar el estrés. Si quieres, agenda tu próxima sesión en la app. Responde STOP "
    "para no recibir más mensajes."
)
COPING_TEST: dict[str, Any] = {
    "name": "Afrontamiento del estrés (diseñado por la Dra. Vera)",
    "description": "Cuestionario propio de la práctica, para seguimiento entre sesiones.",
    "instructions": "Piense en la última semana. No hay respuestas correctas o incorrectas.",
    "administration": "professional",
    "items": [
        {
            "id": "apoyo",
            "text": "Busco apoyo cuando lo necesito",
            "options": [
                {"label": "Nunca", "value": 0},
                {"label": "A veces", "value": 1},
                {"label": "A menudo", "value": 2},
                {"label": "Siempre", "value": 3},
            ],
        },
        {
            "id": "evito",
            "text": "Evito pensar en lo que me preocupa",
            "reverse": True,
            "options": [
                {"label": "Nunca", "value": 0},
                {"label": "A veces", "value": 1},
                {"label": "A menudo", "value": 2},
                {"label": "Siempre", "value": 3},
            ],
        },
        {
            "id": "pausa",
            "text": "Hago pausas para respirar o relajarme",
            "options": [
                {"label": "Nunca", "value": 0},
                {"label": "A veces", "value": 1},
                {"label": "A menudo", "value": 2},
                {"label": "Siempre", "value": 3},
            ],
        },
        {
            "id": "sueno",
            "text": "Horas de sueño por noche (promedio)",
            "type": "number",
            "min": 0,
            "max": 24,
        },
        {"id": "nota", "text": "¿Qué le ayudó más esta semana?", "type": "text", "required": False},
    ],
    "scoring": {
        "method": "sum",
        "subscales": {"activo": ["apoyo", "pausa"], "evitacion": ["evito"]},
        "bands": [
            {"min": 0, "max": 5, "label": "afrontamiento bajo", "severity": "moderate"},
            {"min": 5.01, "max": 99, "label": "afrontamiento adecuado", "severity": "none"},
        ],
        "alerts": [
            {
                "item": "sueno",
                "op": "lte",
                "value": 4,
                "message": "Sueño muy escaso: explorar en la próxima sesión",
            }
        ],
    },
    "licence": {
        "source": "own",
        "attestation": True,
        "note": "Instrumento propio de la práctica (sintético para la demo).",
    },
}
SIGNED_CONSENT_PDF = (
    b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Count 0/Kids[]>>endobj\n"
    b"% Consentimiento informado firmado (documento sintetico de demostracion)\n"
    b"trailer<</Root 1 0 R>>\n%%EOF\n"
)
KNOWLEDGE = [
    (
        "Horarios y ubicación",
        "Atendemos de lunes a viernes de 08:00 a 19:00 y sábados de "
        "09:00 a 13:00. Consultas presenciales en el centro y en línea por videollamada.",
    ),
    (
        "Precios",
        "Primera consulta de evaluación: desde 35 USD. Sesión de psicoterapia "
        "individual: desde 30 USD. Paquetes de 4 sesiones con descuento.",
    ),
    (
        "Política de cancelación",
        "Puede reprogramar o cancelar sin costo con 24 horas de "
        "anticipación desde la app o escribiendo a recepción.",
    ),
]


class DemoError(RuntimeError):
    """A step of the demo did not answer as expected (the message names it)."""


class Demo:
    def __init__(
        self,
        client: httpx.Client,
        service_key: str,
        *,
        meta_app_secret: str | None = None,
        telegram_chat_id: str | None = None,
        accounts: dict[str, str] | None = None,
        seed: int = 7,
    ) -> None:
        self.c = client
        self.service = {"X-API-Key": service_key}
        self.meta_app_secret = meta_app_secret
        self.telegram_chat_id = telegram_chat_id
        # Real ids per network (WhatsApp phone-number id, Instagram user id, Facebook Page
        # id): with the matching SOCIAL_SECRET_* the publications and replies go live.
        self.accounts = {k: v for k, v in (accounts or {}).items() if v}
        self.rng = random.Random(seed)  # noqa: S311 - synthetic data, not security
        self.keys: dict[str, str] = {}
        self.report: list[dict[str, Any]] = []
        self.suffix = datetime.now(UTC).strftime("%H%M%S")

    # --- plumbing ---------------------------------------------------------------------

    def call(
        self,
        method: str,
        path: str,
        who: str = "service",
        ok: tuple[int, ...] = (200, 201, 204),
        **kwargs: Any,
    ) -> Any:
        headers = self.service if who == "service" else {"X-API-Key": self.keys[who]}
        for _ in range(6):  # a well-behaved client: honour 429 and 503 Retry-After
            response = self.c.request(method, path, headers=headers, **kwargs)
            if response.status_code not in (429, 503) or response.status_code in ok:
                break
            time.sleep(min(float(response.headers.get("Retry-After", "2")), 60))
        if response.status_code not in ok:
            raise DemoError(
                f"{method} {path} as {who}: {response.status_code} {response.text[:300]}"
            )
        if not response.content or response.headers.get("content-type", "").startswith(
            "application/pdf"
        ):
            return None
        return response.json()

    def step(self, name: str, **facts: Any) -> None:
        self.report.append({"step": name, **facts})

    # --- the flows ----------------------------------------------------------------------

    def team(self) -> None:
        roles = {
            "dra.vera": ["reviewer"],  # psychologist
            "dr.ruiz": ["reviewer"],  # psychiatrist
            "recepcion": ["reception"],
            "marketing": ["marketing"],
            "direccion": ["owner", "privacy"],
        }
        for name, granted in roles.items():
            staff = self.call(
                "POST", "/v1/admin/staff", json={"name": f"{name}.{self.suffix}", "roles": granted}
            )
            self.keys[name] = staff["key"]
        for pid, pack in (("dra.vera", "ec-psychologist"), ("dr.ruiz", "ec-psychiatrist")):
            self.call(
                "POST",
                "/v1/admin/professionals",
                json={
                    "professional_id": f"{pid}.{self.suffix}",
                    "display_name": {"dra.vera": "Dra. Vera", "dr.ruiz": "Dr. Ruiz"}[pid],
                    "pack_id": pack,
                    "staff_id": f"{pid}.{self.suffix}",
                },
            )
        self.step("team", staff=len(roles), professionals=2)

    def patients(self) -> list[str]:
        now = datetime.now(UTC)
        ids = []
        for n in range(20):
            pid = f"demo-{self.suffix}-{n:02d}"
            first, last = FIRST[n], self.rng.choice(LAST)
            # Live mode: ONLY your own chat gets a Telegram id. Invented ids could belong
            # to real people, and the campaign would write to them. In a rehearsal nothing
            # is sent, so synthetic ids are harmless there.
            if self.telegram_chat_id:
                telegram = self.telegram_chat_id if n == 0 else None
            else:
                telegram = str(900000000 + n)
            self.call(
                "POST",
                "/v1/crm/patients",
                json={
                    "id": pid,
                    "display_name": f"{first} {last}",
                    "phone": f"+1 555 01{n:02d}",
                    "email": f"{first.lower()}.{n}@example.test",
                    "telegram_chat_id": telegram,
                },
            )
            # Half are "dormant" (last visit over a year ago): the engagement campaign's audience.
            days_ago = 400 if n % 2 == 0 else self.rng.randint(10, 60)
            visit = self.call(
                "POST",
                "/v1/crm/appointments",
                json={
                    "patient_id": pid,
                    "starts_at": (now - timedelta(days=days_ago)).isoformat(),
                    "kind": "sesion",
                    "price": 30,
                },
            )
            self.call(
                "POST",
                f"/v1/crm/appointments/{visit['id']}/status",
                json={"status": "completed"},
            )
            if n % 3 == 0:  # an upcoming session
                self.call(
                    "POST",
                    "/v1/crm/appointments",
                    json={
                        "patient_id": pid,
                        "starts_at": (now + timedelta(days=3 + n)).isoformat(),
                        "kind": "sesion",
                        "price": 30,
                    },
                )
            for purpose, granted in (("marketing", n % 2 == 0), ("analytics", n % 4 != 3)):
                self.call(
                    "PUT",
                    f"/v1/subjects/{pid}/consents/{purpose}",
                    json={"granted": granted, "source": "app (demo)"},
                )
            ids.append(pid)
        self.step("patients", created=len(ids), dormant=len(ids) // 2)
        return ids

    def agenda(self, ids: list[str]) -> None:
        """Working hours for both professionals, a patient booking from the app, and the
        calendar links for their phones."""
        weekdays = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
        hours = {
            "dra.vera": [{"day": d, "hours": "09:00-13:00", "slot_minutes": 50} for d in weekdays]
            + [{"day": d, "hours": "15:00-19:00", "slot_minutes": 50} for d in weekdays[:5]],
            "dr.ruiz": [
                {"day": d, "hours": "14:00-18:00", "slot_minutes": 30} for d in weekdays[:5]
            ],
        }
        for name, blocks in hours.items():
            self.call(
                "PUT",
                f"/v1/agenda/professionals/{name}.{self.suffix}/hours",
                json={"hours": blocks},
            )
        vera = f"dra.vera.{self.suffix}"
        access = self.call("POST", f"/v1/crm/patients/{ids[1]}/access")
        self.keys["paciente2"] = access["key"]
        slots = self.call("GET", f"/v1/me/slots?professional_id={vera}", "paciente2")
        booked = self.call(
            "POST",
            "/v1/me/appointments",
            "paciente2",
            json={"professional_id": vera, "starts_at": slots[0]["starts_at"]},
        )
        # The same time for someone else is refused: no double booking.
        clash = self.c.post(
            "/v1/crm/appointments",
            headers=self.service,
            json={
                "patient_id": ids[2],
                "starts_at": booked["starts_at"],
                "professional_id": vera,
            },
        )
        link = self.call("POST", f"/v1/agenda/professionals/{vera}/calendar-link", "recepcion")
        self.step(
            "agenda",
            professionals_with_hours=len(hours),
            free_slots_next_14_days=len(slots),
            patient_booked=slots[0]["local"],
            double_booking=f"refused ({clash.status_code})",
            calendar_feed=link["url"].split("/calendar/")[0] + "/calendar/…",
        )

    def clinical(self, patient: str) -> None:
        self.call(
            "POST",
            f"/v1/clinical/patients/{patient}/documents",
            "dra.vera",
            json={
                "doc_type": "session_note",
                "body": "Sesión 1: motivo de consulta, estrés laboral.",
            },
        )
        self.call(
            "POST",
            f"/v1/clinical/patients/{patient}/documents",
            "dra.vera",
            json={"doc_type": "psychotherapy_note", "body": "Hipótesis de trabajo (solo autora)."},
        )
        custom = self.call(
            "POST", "/v1/clinical/instruments", "dra.vera", json={"spec": COPING_TEST}
        )
        own = self.call(
            "POST",
            f"/v1/clinical/patients/{patient}/instrument-results",
            "dra.vera",
            json={
                "instrument_id": custom["instrument_id"],
                "answers": {"apoyo": 1, "evito": 3, "pausa": 1, "sueno": 4, "nota": "caminar"},
            },
        )
        phq9 = self.call(
            "POST", "/v1/clinical/instruments/from-template", "dra.vera", json={"template": "phq9"}
        )
        flagged = self.call(
            "POST",
            f"/v1/clinical/patients/{patient}/instrument-results",
            "dra.vera",
            json={
                "instrument_id": phq9["instrument_id"],
                "answers": {f"i{n}": 1 for n in range(1, 9)} | {"i9": 1},
            },
        )
        gad7 = self.call(
            "POST", "/v1/clinical/instruments/from-template", "dra.vera", json={"template": "gad7"}
        )
        self.call(
            "POST",
            f"/v1/clinical/patients/{patient}/instrument-results",
            "dra.vera",
            json={
                "instrument_id": gad7["instrument_id"],
                "answers": {f"i{n}": 2 for n in range(1, 8)},
            },
        )
        attached = self.call(
            "POST",
            f"/v1/clinical/patients/{patient}/files",
            "dra.vera",
            files={"file": ("consentimiento-firmado.pdf", SIGNED_CONSENT_PDF, "application/pdf")},
            data={"label": "Consentimiento informado firmado"},
        )
        self.step(
            "clinical record",
            custom_test=f"{own['instrument_name']}: {own['total']} ({own['band']})",
            custom_test_alerts=[a["message"] for a in own["alerts"]],
            phq9=f"{flagged['total']} ({flagged['band']})",
            phq9_alerts=[a["message"] for a in flagged["alerts"]],
            file_sha256=attached["sha256"][:16] + "…",
        )

    def knowledge(self) -> None:
        for title, text in KNOWLEDGE:
            self.call("POST", "/v1/knowledge/documents", json={"title": title, "text": text})
        self.step("knowledge base", documents=len(KNOWLEDGE))

    def assistant(self, patient: str) -> None:
        everyday = self.call(
            "POST",
            "/v1/chat",
            "recepcion",
            json={"question": "¿Cuál es la política de cancelación de citas?"},
        )
        held = self.call(
            "POST",
            "/v1/chat",
            "recepcion",
            json={
                "question": "La paciente pregunta qué dosis de sertralina debería tomar",
                "subject_id": patient,
                "thread_id": f"demo-held-{self.suffix}",
            },
        )
        status = held["status"]
        if status == "pending_review":
            self.call(
                "POST",
                f"/v1/reviews/demo-held-{self.suffix}",
                "dra.vera",
                json={
                    "approved": True,
                    "edited_answer": "La dosis la indica únicamente su médico tratante. "
                    "Le sugerimos agendar una cita con el Dr. Ruiz.",
                },
            )
        after = self.call("GET", f"/v1/threads/demo-held-{self.suffix}", "recepcion")
        self.step(
            "assistant",
            everyday=everyday["provenance"],
            clinical_question=status,
            after_review=after["provenance"],
        )

    def patient_view(self, patient: str) -> None:
        access = self.call("POST", f"/v1/crm/patients/{patient}/access")
        self.keys["paciente"] = access["key"]
        me = self.call("GET", "/v1/me", "paciente")
        answer = self.call(
            "POST", "/v1/me/chat", "paciente", json={"question": "¿Cuándo es mi próxima cita?"}
        )
        self.step(
            "patient app (API)",
            upcoming=len(me.get("upcoming_appointments", [])),
            chat=answer["status"],
            provenance=answer["provenance"],
        )

    def campaign(self) -> None:
        mode = "live" if self.telegram_chat_id else "simulation"
        created = self.call(
            "POST",
            "/v1/campaigns",
            "marketing",
            json={
                "name": f"Engagement: cuidar el sueño ({self.suffix})",
                "kind": "education",
                "segment": "dormant",
                "template": TEMPLATE,
                "mode": mode,
                "holdout_pct": 30,  # a visible control group even with a small audience
            },
        )
        cid = created["id"]
        self.call("POST", f"/v1/campaigns/{cid}/approve", "direccion", json={})
        sent = self.call("POST", f"/v1/campaigns/{cid}/send", "marketing")
        results = self.call("GET", f"/v1/campaigns/{cid}/results", "direccion")
        self.step(
            "engagement campaign",
            mode=mode,
            outcomes=sent.get("outcomes"),
            eligible=results.get("population", {}).get("eligible"),
            control_group=f"{(sent.get('outcomes') or {}).get('held_out', 0)} patients not "
            "contacted, to measure the campaign's real lift (random 30 %)",
        )

    def social(self) -> None:
        accounts = {
            "instagram": ("demo_consultorio", "IG_DEMO"),
            "facebook": ("Consultorio Demo", "FB_DEMO"),
            "tiktok": ("@consultorio.demo", "TT_DEMO"),
            "telegram": ("@ConsultorioDemoBot", "TG_DEMO"),
            "whatsapp": ("+1 555 0100", "WA_DEMO"),
        }
        ids = {}
        for network, (handle, secret_ref) in accounts.items():
            external = self.accounts.get(network) or (
                WA_NUMBER_ID if network == "whatsapp" else f"{network}-{self.suffix}"
            )
            made = self.call(
                "POST",
                "/v1/social/accounts",
                json={
                    "network": network,
                    "external_id": external,
                    "handle": handle,
                    "secret_ref": secret_ref,
                },
                ok=(201, 409),
            )
            if made:
                ids[network] = made.get("account_id")
        published = {}
        for network in ("instagram", "facebook", "tiktok"):
            if not ids.get(network):
                continue
            kind = "video" if network == "tiktok" else "infographic"
            pub = self.call(
                "POST",
                "/v1/social/publications",
                "marketing",
                json={
                    "account_id": ids[network],
                    "kind": kind,
                    "caption": "Dormir bien también es salud mental. Tres hábitos sencillos "
                    "para esta semana.",
                    "brief": {
                        "title": "Cuidar el sueño",
                        "points": ["Horario regular", "Pantallas fuera de la cama", "Pausas"],
                        "cta": "Agenda en la app",
                        "practice_name": "Consultorio Demo",
                    },
                },
                ok=(201, 409, 422, 503),
            )
            if not pub or "publication_id" not in pub:
                # Refused on purpose (e.g. Facebook rules not verified yet, decision A6;
                # video needs ffmpeg): the report says why instead of pretending.
                published[network] = f"refused: {(pub or {}).get('detail', 'unavailable')}"
                continue
            pid = pub["publication_id"]
            self.call("POST", f"/v1/social/publications/{pid}/approve", "direccion")
            out = self.call(
                "POST", f"/v1/social/publications/{pid}/publish", "marketing", ok=(200, 409, 502)
            )
            out = out or {}
            published[network] = (
                "LIVE: published on the network"
                if out.get("mode") == "live" and out.get("status") == "published"
                else "dry run: no credentials, nothing sent (request recorded)"
                if out.get("mode") == "dry_run"
                else f"{out.get('status', '?')}: {out.get('error') or ''}".strip()
            )
        self.step("social media", accounts=sorted(ids), publications=published)

    def crisis(self) -> None:
        if "whatsapp" in self.accounts:
            # Live WhatsApp: a rehearsal from an invented number would make the warm reply
            # go to a stranger. The owner writes from their own phone instead.
            self.call(
                "POST",
                "/v1/admin/on-call",
                json={
                    "display_name": "Guardia (demo)",
                    "level": 1,
                    "email": "guardia@example.test",
                },
            )
            self.step(
                "crisis alert",
                live="write to your WhatsApp test number from your phone: a warm reply "
                "arrives at once and an alert opens for the on-call professional",
            )
            return
        if not self.meta_app_secret:
            self.step(
                "crisis alert", skipped="start the server with META_APP_SECRET to rehearse it"
            )
            return
        self.call(
            "POST",
            "/v1/admin/on-call",
            json={
                "display_name": "Dra. Vera (guardia)",
                "level": 1,
                "email": "guardia@example.test",
            },
        )
        message = {
            "from": "15550009999",
            "id": f"wamid.demo.{self.suffix}",
            "timestamp": str(int(datetime.now(UTC).timestamp())),
            "type": "text",
            "text": {"body": CRISIS_TEXT},
        }
        value = {
            "messaging_product": "whatsapp",
            "metadata": {"display_phone_number": "15550000100", "phone_number_id": WA_NUMBER_ID},
            "contacts": [{"profile": {"name": "Sintético"}, "wa_id": "15550009999"}],
            "messages": [message],
        }
        body = json.dumps(
            {
                "object": "whatsapp_business_account",
                "entry": [{"id": "WABA", "changes": [{"field": "messages", "value": value}]}],
            }
        ).encode()
        signature = (
            "sha256=" + hmac.new(self.meta_app_secret.encode(), body, hashlib.sha256).hexdigest()
        )
        response = self.c.post(
            "/v1/channels/whatsapp",
            content=body,
            headers={"X-Hub-Signature-256": signature, "Content-Type": "application/json"},
        )
        alerts = self.call("GET", "/v1/social/alerts", "dra.vera")
        self.step("crisis alert", webhook=response.status_code, open_alerts=len(alerts))

    def follow_up(self, patient: str) -> None:
        view = self.call("GET", f"/v1/clinical/patients/{patient}/follow-up", "dra.vera")
        worklist = self.call("GET", "/v1/clinical/follow-up", "dra.vera")
        self.step(
            "follow-up",
            attendance=view["attendance"]["attendance_rate"],
            no_show_risk=view["no_show_risk"]["level"],
            tests=[f"{t['instrument']}: {t['direction']}" for t in view["tests"]],
            flags=view["flags"],
            needing_attention=len(worklist),
        )

    def rights(self, keep: str, erase: str) -> None:
        exported = self.call("GET", f"/v1/subjects/{keep}/export", "direccion")
        erased = self.call("DELETE", f"/v1/subjects/{erase}", "direccion")
        verify = self.call("GET", "/v1/audit/verify", "direccion")
        self.step(
            "privacy",
            export_stores=sorted(exported.get("inventory", {})),
            erasure_retained=[r["store"] for r in erased.get("retained", [])],
            audit_chain=verify,
        )

    def run(self) -> dict[str, Any]:
        self.team()
        ids = self.patients()
        self.agenda(ids)
        self.clinical(ids[0])
        self.knowledge()
        self.assistant(ids[0])
        self.patient_view(ids[0])
        self.campaign()
        self.social()
        self.crisis()
        self.follow_up(ids[0])
        self.rights(keep=ids[0], erase=ids[19])
        return {"keys": self.keys, "report": self.report}
