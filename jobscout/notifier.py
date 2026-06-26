"""Gmail digest email for the selected roles."""
from __future__ import annotations

import smtplib
import ssl
from email.message import EmailMessage

from .models import Job, Score


class EmailNotifier:
    """Sends one digest per run via Gmail SMTP. Validates creds on send."""

    def __init__(self, user: str, app_password: str, mail_to: str,
                 host: str = "smtp.gmail.com", port: int = 587):
        self._user = user
        self._app_password = app_password
        self._mail_to = mail_to
        self._host = host
        self._port = port

    def send_digest(self, items: list[tuple[Job, Score]], subject: str | None = None) -> None:
        if not items:
            return
        if not (self._user and self._app_password and self._mail_to):
            raise RuntimeError("GMAIL_USER / GMAIL_APP_PASSWORD / MAIL_TO not all set")

        message = EmailMessage()
        message["Subject"] = subject or f"[Job Scout] {len(items)} new roles"
        message["From"] = self._user
        message["To"] = self._mail_to
        message.set_content(self._body(items))

        context = ssl.create_default_context()
        with smtplib.SMTP(self._host, self._port) as server:
            server.starttls(context=context)
            server.login(self._user, self._app_password)
            server.send_message(message)

    @staticmethod
    def _body(items: list[tuple[Job, Score]]) -> str:
        lines: list[str] = []
        for job, score in items:
            lines.append(f"{job.title} — {job.company}")
            location = job.location or "?"
            department = job.department or "?"
            posted = job.date_posted or "?"
            lines.append(f"  location: {location} | dept: {department} | posted: {posted}")
            lines.append(
                f"  computer-vision: {score.computer_vision_score} | "
                f"experience: {score.experience_score}"
            )
            lines.append(f"  why: {score.reason}")
            lines.append(f"  {job.url}")
            lines.append("")
        return "\n".join(lines)
