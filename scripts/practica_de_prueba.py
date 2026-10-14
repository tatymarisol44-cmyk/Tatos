"""A practice full of synthetic data, on this computer only, to try every screen.

    .venv\\Scripts\\python.exe scripts\\practica_de_prueba.py

Starts the real app on http://127.0.0.1:8800 with its own throw-away database (in
%USERPROFILE%\\agency-demo), runs `agency demo` against it (patients, visits, notes,
documents, tests, Instagram/Facebook/WhatsApp messages, a crisis, a campaign), writes the
links and the synthetic keys to Desktop\\PRACTICA-DE-PRUEBA.txt and keeps serving until
Ctrl+C. Nothing here touches the production service or its database. Every run starts
from an empty database."""

from __future__ import annotations

import secrets
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


def main() -> int:
    HOME.mkdir(exist_ok=True)
    database = HOME / "demo.db"
    database.unlink(missing_ok=True)  # a fresh practice every time
    service_key = f"svc_demo_{secrets.token_hex(12)}"
    meta_secret = secrets.token_hex(16)
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
    )
    app = create_app(settings, Orchestrator(settings))
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
    keys = out["keys"]
    SHEET.write_text(
        "PRÁCTICA DE PRUEBA (datos ficticios): solo en esta computadora\n"
        "================================================================\n\n"
        f"App del consultorio:  {BASE}/app\n"
        f"  Llave de la Dra. Vera (psicóloga ficticia):  {keys['dra.vera']}\n"
        f"  Llave de Dirección (dueña ficticia):          {keys['direccion']}\n\n"
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
