"""Transactional email via pluggable providers (console for dev, SMTP; add Resend/SES easily)."""

from __future__ import annotations

import logging
import re
import smtplib
from email.message import EmailMessage
from typing import Protocol

from pydantic import BaseModel, field_validator

EMAIL_RE = re.compile(r"^[^@\s<>\"]+@[^@\s<>\"]+\.[^@\s<>\"]+$")

log = logging.getLogger("redblue.email")


class Email(BaseModel):
    to: str
    subject: str
    text: str
    html: str | None = None

    @field_validator("to")
    @classmethod
    def _valid(cls, v: str) -> str:
        v = v.strip()
        if len(v) > 320 or not EMAIL_RE.match(v) or "\n" in v or "\r" in v:
            raise ValueError("Invalid email address.")
        return v


class EmailSender(Protocol):
    def send(self, email: Email) -> None: ...


class ConsoleEmail:
    """Logs emails and keeps them in memory (inspectable in tests and dev)."""

    def __init__(self, sender: str):
        self.sender = sender
        self.outbox: list[Email] = []

    def send(self, email: Email) -> None:
        self.outbox.append(email)
        log.info("email to=%s subject=%s", email.to, email.subject)


class SMTPEmail:
    def __init__(self, sender: str, host: str, port: int, user: str | None, password: str | None):
        self.sender, self.host, self.port = sender, host, port
        self.user, self.password = user, password

    def send(self, email: Email) -> None:
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = self.sender, email.to, email.subject
        msg.set_content(email.text)
        if email.html:
            msg.add_alternative(email.html, subtype="html")
        with smtplib.SMTP(self.host, self.port, timeout=15) as smtp:
            smtp.starttls()
            if self.user:
                smtp.login(self.user, self.password or "")
            smtp.send_message(msg)
