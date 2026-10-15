"""A practice full of synthetic data, on this computer only, to try every screen.

    .venv\\Scripts\\python.exe scripts\\practica_de_prueba.py

Starts the real app on http://127.0.0.1:8800 with its own throw-away database (in
%USERPROFILE%\\agency-demo), runs `agency demo` against it (patients, visits, notes,
documents, tests, Instagram/Facebook/WhatsApp messages, a crisis, a campaign), writes the
links and the synthetic keys to Desktop\\PRACTICA-DE-PRUEBA.txt and keeps serving until
Ctrl+C. Nothing here touches the production service or its database. Every run starts
from an empty database.

E-mail sign-in (ADR 0024) works too: a synthetic account like the owner's gets an e-mail
and a password, and every e-mail the app "sends" (codes, invitations) is written as a text
file to Desktop\\Correos de prueba."""

from __future__ import annotations

import re
import secrets
import shutil
import sys
import threading
import time
from pathlib import Path

import httpx
import uvicorn

from orchestrator.api.app import create_app
from orchestrator.config import Settings
from orchestrator.demo import Demo
from orchestrator.service import Orchestrator

PORT = 8800
BASE = f"http://127.0.0.1:{PORT}"
HOME = Path.home() / "agency-demo"
SHEET = Path.home() / "Desktop" / "PRACTICA-DE-PRUEBA.txt"
OUTBOX = Path.home() / "Desktop" / "Correos de prueba"
EMAIL = "carla@consultorio-prueba.test"


def main() -> int:
    HOME.mkdir(exist_ok=True)
    database = HOME / "demo.db"
    database.unlink(missing_ok=True)  # a fresh practice every time
    shutil.rmtree(OUTBOX, ignore_errors=True)
    service_key = f"svc_demo_{secrets.token_hex(12)}"
    meta_secret = secrets.token_hex(16)
    password = f"Prueba-{secrets.token_hex(4)}"  # a new one each run, shown in the sheet
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        app_env="dev",
        database_url=f"sqlite+aiosqlite:///{database.as_posix()}",  # type: ignore[arg-type]
        api_keys=f"{service_key}:demo",  # type: ignore[arg-type]
        tenant_packs={"demo": "ec-psychologist"},
        practice_display_name="Consultorio de prueba",
        llm_backend="fake",
        embedding_backend="hashing",
        vector_backend="memory",
        meta_app_secret=meta_secret,  # type: ignore[arg-type]
        whatsapp_verify_token=secrets.token_hex(8),  # type: ignore[arg-type]
        media_dir=HOME / "media",
        public_base_url=BASE,
        rate_limit_per_minute=10_000,
        dev_outbox_dir=str(OUTBOX),
    )
    orch = Orchestrator(settings)
    app = create_app(settings, orch)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    deadline = time.time() + 120
    while not server.started and time.time() < deadline:
        time.sleep(0.2)
    if not server.started:
        print("the app did not start (is port 8800 in use?)", file=sys.stderr)
        return 1
    print("Filling the practice with synthetic data (about a minute)...")
    with httpx.Client(base_url=BASE, timeout=180) as client:
        out = Demo(client, service_key, meta_app_secret=meta_secret).run()
        # An account like the owner's, signing in with e-mail: invited, then its password set
        # from the invitation link (the e-mail lands in the outbox folder too).
        made = client.post(
            "/v1/admin/team",
            json={
                "name": "Carla (prueba)",
                "email": EMAIL,
                "roles": ["admin", "owner", "reviewer"],
            },
            headers={"X-API-Key": service_key},
        )
        made.raise_for_status()
        link = re.search(r"#clave=([\w-]+)", orch.mailer.outbox[-1].text)
        assert link is not None
        chosen = {"token": link.group(1), "password": password}
        client.post("/v1/auth/password", json=chosen).raise_for_status()
    keys = out["keys"]
    SHEET.write_text(
        "PRÁCTICA DE PRUEBA (datos ficticios): solo en esta computadora\n"
        "================================================================\n\n"
        f"App del consultorio:  {BASE}/app\n\n"
        "  Entrar con correo (cuenta como la de Carla: dueña, administración y psicóloga):\n"
        f"    Correo:      {EMAIL}\n"
        f"    Contraseña:  {password}\n"
        "    El código de 6 números llega como archivo a la carpeta «Correos de prueba»\n"
        "    de tu escritorio: abre el archivo más nuevo.\n\n"
        "  Llaves de emergencia (botón «Entrar con llave de emergencia»):\n"
        f"    Dra. Vera (psicóloga ficticia):  {keys['dra.vera']}\n"
        f"    Dirección (dueña ficticia):      {keys['direccion']}\n\n"
        f"App de la paciente (ábrela en el navegador):\n  {BASE}/mi/#k={keys['paciente']}\n\n"
        "Todo es inventado y vive en esta computadora: no toca tu sistema real.\n"
        "La práctica funciona mientras la ventana negra que la abrió siga abierta.\n",
        encoding="utf-8",
    )
    for step in out["report"]:
        print("-", step.get("step"))
    print(f"\nReady: {BASE}/app  (links and keys in {SHEET})")
    print("Press Ctrl+C to close it.")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        server.should_exit = True
    return 0


if __name__ == "__main__":
    sys.exit(main())
