"""Forms, newsletter double opt-in with stored consent, and leads.

Subscribers are only added after they confirm by email. Every subscriber row stores the
consent text they agreed to, when, and on which page."""

from __future__ import annotations

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from pydantic import BaseModel, Field, ValidationError, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from redblue.core.db import utcnow
from redblue.core.email import EMAIL_RE, Email
from redblue.growth.models import Lead, Subscriber

CONSENT_TEXT = "Email me updates. I can unsubscribe at any time."


class FormInput(BaseModel):
    email: str = Field(max_length=320)
    name: str = Field(default="", max_length=200)
    message: str = Field(default="", max_length=5000)
    consent: str = ""
    website: str = ""  # honeypot
    lead_magnet: str = Field(default="", max_length=200)

    @field_validator("email")
    @classmethod
    def _email(cls, v: str) -> str:
        v = v.strip().lower()
        if not EMAIL_RE.match(v):
            raise ValueError("Please enter a valid email address.")
        return v


class FormError(ValueError):
    pass


def _signer(secret: str, salt: str) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(secret, salt=salt)


def handle_form(
    db: Session,
    kind: str,
    data: dict,
    *,
    secret: str,
    base_url: str,
    site_name: str,
    email_sender,
    source_path: str,
    notify: str | None,
) -> str:
    """Returns a user-facing message. Raises FormError for invalid input."""
    try:
        form = FormInput(**{k: v for k, v in data.items() if k in FormInput.model_fields})
    except ValidationError as exc:
        raise FormError(exc.errors()[0]["msg"].removeprefix("Value error, ")) from exc
    if form.website:  # bots fill hidden fields; pretend success
        return "Thanks!"
    if kind == "contact":
        if not form.message.strip():
            raise FormError("Please include a message.")
        db.add(
            Lead(
                kind="contact",
                name=form.name,
                email=form.email,
                message=form.message,
                source_path=source_path[:500],
            )
        )
        if notify:
            email_sender.send(
                Email(
                    to=notify,
                    subject=f"New contact form message ({site_name})",
                    text=f"From: {form.name} <{form.email}>\n\n{form.message}",
                )
            )
        return "Thanks, we'll get back to you soon."
    if kind not in ("newsletter", "waitlist", "lead_magnet"):
        raise FormError("Unknown form.")
    if form.consent != "yes":
        raise FormError("Please tick the consent box so we can email you.")
    sub = db.scalar(select(Subscriber).where(Subscriber.email == form.email))
    if sub is None:
        sub = Subscriber(
            email=form.email,
            list_name=kind,
            consent_text=CONSENT_TEXT,
            consent_source=source_path[:500],
            lead_magnet=form.lead_magnet or None,
        )
        db.add(sub)
        db.flush()
    elif sub.status == "confirmed":
        return "You're already subscribed. Thanks!"
    if kind in ("lead_magnet", "waitlist"):
        db.add(Lead(kind=kind, name=form.name, email=form.email, source_path=source_path[:500]))
    token = _signer(secret, "rb-confirm").dumps(sub.email)
    link = f"{base_url}/newsletter/confirm/{token}"
    email_sender.send(
        Email(
            to=sub.email,
            subject=f"Confirm your subscription to {site_name}",
            text=f"Please confirm your subscription to {site_name}:\n\n{link}\n\n"
            "If you didn't ask for this, ignore this email and you won't hear from us.",
        )
    )
    return "Almost done: check your inbox to confirm your email address."


def confirm(db: Session, token: str, secret: str, max_age: int = 7 * 86400) -> Subscriber | None:
    try:
        email = _signer(secret, "rb-confirm").loads(token, max_age=max_age)
    except (BadSignature, SignatureExpired):
        return None
    sub = db.scalar(select(Subscriber).where(Subscriber.email == email))
    if sub and sub.status != "confirmed":
        sub.status, sub.confirmed_at, sub.unsubscribed_at = "confirmed", utcnow(), None
    return sub


def unsubscribe_token(email: str, secret: str) -> str:
    return _signer(secret, "rb-unsub").dumps(email)


def unsubscribe(db: Session, token: str, secret: str) -> bool:
    try:
        email = _signer(secret, "rb-unsub").loads(token)  # no expiry: links must keep working
    except BadSignature:
        return False
    sub = db.scalar(select(Subscriber).where(Subscriber.email == email))
    if sub:
        sub.status, sub.unsubscribed_at = "unsubscribed", utcnow()
    return True


def lead_magnet_link(storage, key: str, ttl: int = 86400) -> str | None:
    signer = getattr(storage, "signed_url", None)
    return signer(key, ttl) if signer else None
