"""Render the Alertmanager configuration of Google Managed Prometheus (decision O1) as the
Kubernetes Secret it reads (`gmp-public/alertmanager`). Credentials come from the
environment, never from git:

    TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=... \\
    [ALERT_EMAIL_TO=... SMTP_HOST=host:587 SMTP_FROM=... SMTP_USERNAME=... SMTP_PASSWORD=...] \\
    python deploy/monitoring/render_alertmanager.py | kubectl apply -f -

Routing (docs/runbooks/README.md, "Who is paged"):
- `severity=page`: Telegram to the platform on-call (and e-mail when configured), every
  hour until resolved;
- `severity=ticket`: e-mail once a day while firing; Telegram when there is no e-mail, so a
  ticket is never dropped.

Alerts carry only rule labels (alert name, SLO): never patient data."""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from typing import Any

import yaml

EMAIL_KEYS = ("ALERT_EMAIL_TO", "SMTP_HOST", "SMTP_FROM", "SMTP_USERNAME", "SMTP_PASSWORD")


def _config(env: Mapping[str, str]) -> dict[str, Any]:
    missing = [k for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID") if not env.get(k)]
    if missing:
        raise SystemExit(f"missing {', '.join(missing)}: pages need a Telegram chat")
    telegram = {
        "bot_token": env["TELEGRAM_BOT_TOKEN"],
        "chat_id": int(env["TELEGRAM_CHAT_ID"]),
        "send_resolved": True,
    }
    email = None
    if all(env.get(k) for k in EMAIL_KEYS):
        email = {
            "to": env["ALERT_EMAIL_TO"],
            "from": env["SMTP_FROM"],
            "smarthost": env["SMTP_HOST"],
            "auth_username": env["SMTP_USERNAME"],
            "auth_password": env["SMTP_PASSWORD"],
            "require_tls": True,
            "send_resolved": True,
        }
    page: dict[str, Any] = {"name": "page", "telegram_configs": [telegram]}
    ticket: dict[str, Any] = {"name": "ticket"}
    if email:
        page["email_configs"] = [email]
        ticket["email_configs"] = [email]
    else:
        ticket["telegram_configs"] = [telegram]
    return {
        "route": {
            "receiver": "page",  # an alert without a known severity still reaches a person
            "group_by": ["alertname", "slo"],
            "group_wait": "30s",
            "group_interval": "5m",
            "repeat_interval": "1h",
            "routes": [
                {"matchers": ['severity="page"'], "receiver": "page", "repeat_interval": "1h"},
                {
                    "matchers": ['severity="ticket"'],
                    "receiver": "ticket",
                    "group_wait": "5m",
                    "repeat_interval": "24h",
                },
            ],
        },
        "receivers": [page, ticket],
    }


def render(env: Mapping[str, str]) -> str:
    secret = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": "alertmanager", "namespace": "gmp-public"},
        "type": "Opaque",
        "stringData": {"alertmanager.yaml": yaml.safe_dump(_config(env), sort_keys=False)},
    }
    return yaml.safe_dump(secret, sort_keys=False, width=100)


if __name__ == "__main__":
    sys.stdout.write(render(os.environ))
