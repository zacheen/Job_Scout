"""Gmail digest email — one message per run, grouped into per-track sections."""
from __future__ import annotations

import smtplib
import ssl
from email.message import EmailMessage

from .protocols import Sections


class EmailNotifier:
    """Sends ONE digest per run via Gmail SMTP. Validates creds on send."""

    def __init__(self, user: str, app_password: str, mail_to: str,
                 host: str = "smtp.gmail.com", port: int = 587):
        self._user = user
        self._app_password = app_password
        self._mail_to = mail_to
        self._host = host
        self._port = port

    def send_digest(self, sections: Sections, subject: str | None = None) -> None:
        total = sum(len(items) for _, items in sections)
        if total == 0:
            return
        if not (self._user and self._app_password and self._mail_to):
            raise RuntimeError("GMAIL_USER / GMAIL_APP_PASSWORD / MAIL_TO not all set")

        message = EmailMessage()
        message["Subject"] = subject or f"[Job Scout] {total} new roles"
        message["From"] = self._user
        message["To"] = self._mail_to
        message.set_content(self._body(sections))

        context = ssl.create_default_context()
        with smtplib.SMTP(self._host, self._port) as server:
            server.starttls(context=context)
            server.login(self._user, self._app_password)
            server.send_message(message)

    @staticmethod
    def _body(sections: Sections) -> str:
        blocks: list[str] = []
        for track_name, items in sections:
            if not items:
                continue
            lines = [f"=== {track_name} ({len(items)}) ===", ""]
            for job, score in items:
                lines.append(f"{job.title} ({job.company})")
                lines.append(
                    f"  location: {job.location or '?'} | dept: {job.department or '?'} | "
                    f"posted: {job.date_posted or '?'}"
                )
                lines.append(f"  relevance: {score.relevance_score} | experience: {score.experience_score}")
                lines.append(f"  why: {score.reason}")
                lines.append(f"  {job.url}")
                lines.append("")
            blocks.append("\n".join(lines))
        return "\n".join(blocks)
