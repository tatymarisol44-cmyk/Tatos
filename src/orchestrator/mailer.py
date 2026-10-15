"""Plain-text e-mail over SMTP with STARTTLS (sign-in codes, invitations, on-call notices).

Without SMTP_HOST nothing leaves the machine. Outside production the message is kept in
`Mailer.outbox` (tests read it) and, when DEV_OUTBOX_DIR is set, written there as a text
file, so the local practice can be used end to end without a mail provider. In
production without SMTP_HOST, `available` is False and the features that need e-mail say
so instead of failing silently."""

from __future__ import annotations

import asyncio
import re
import smtplib
import ssl
from dataclasses import dataclass
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path

from orchestrator.config import Settings


@dataclass(frozen=True)
class Mail:
    to: str
    subject: str
    text: str


class Mailer:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.outbox: list[Mail] = []

    @property
    def available(self) -> bool:
        """E-mail can reach the person: an SMTP server, or the dev outbox."""
        return bool(self.settings.smtp_host) or self.settings.app_env != "prod"

    def _send(self, mail: Mail) -> None:
        s = self.settings
        message = EmailMessage()
        message["From"] = s.smtp_from or s.smtp_username or "no-reply@localhost"
        message["To"] = mail.to
        message["Subject"] = mail.subject
        message.set_content(mail.text)
        with smtplib.SMTP(s.smtp_host or "", s.smtp_port, timeout=s.smtp_timeout_seconds) as smtp:
            smtp.starttls(context=ssl.create_default_context())  # never in clear text
            if s.smtp_username and s.smtp_password:
                smtp.login(s.smtp_username, s.smtp_password.get_secret_value())
            smtp.send_message(message)

    def _keep(self, mail: Mail) -> None:
        self.outbox.append(mail)
        del self.outbox[:-50]
        folder = self.settings.dev_outbox_dir
        if not folder:
            return
        path = Path(folder)
        path.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d %H-%M-%S")
        who = re.sub(r"[^A-Za-z0-9._-]", "_", mail.to)[:60]
        body = f"Para: {mail.to}\nAsunto: {mail.subject}\n\n{mail.text}\n"
        (path / f"{stamp} {who}.txt").write_text(body, encoding="utf-8")

    async def send(self, to: str, subject: str, text: str) -> tuple[str, str | None]:
        """("sent" | "kept" | "dry_run" | "failed", reason). Never raises."""
        mail = Mail(to, subject, text)
        if not self.settings.smtp_host:
            if self.settings.app_env == "prod":
                return "dry_run", None
            await asyncio.to_thread(self._keep, mail)
            return "kept", None
        try:
            await asyncio.to_thread(self._send, mail)
        except (smtplib.SMTPException, OSError) as exc:
            return "failed", type(exc).__name__  # never the server's text: it may echo data
        return "sent", None
