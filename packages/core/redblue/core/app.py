"""Application factory wiring the backend SDK and its secure defaults."""

from __future__ import annotations

from fastapi import FastAPI

from redblue.core.ai import AIClient
from redblue.core.config import Settings
from redblue.core.context import Platform
from redblue.core.db import Database
from redblue.core.email import ConsoleEmail, SMTPEmail
from redblue.core.jobs import JobQueue
from redblue.core.security import CSRFMiddleware, SecurityHeadersMiddleware
from redblue.core.storage import LocalStorage, S3Storage

CSRF_EXEMPT = ("/_rb/beacon", "/webhooks/", "/_rb/health")


def build_platform(settings: Settings) -> Platform:
    db = Database(settings.database_url)
    db.create_all()
    if settings.storage_backend == "s3":
        storage = S3Storage(
            settings.s3_bucket or "", settings.max_upload_bytes, settings.s3_endpoint_url
        )
    else:
        storage = LocalStorage(
            settings.storage_dir, settings.max_upload_bytes, settings.secret_key.get_secret_value()
        )
    if settings.email_provider == "smtp" and settings.smtp_host:
        email = SMTPEmail(
            settings.email_from,
            settings.smtp_host,
            settings.smtp_port,
            settings.smtp_user,
            settings.smtp_password.get_secret_value() if settings.smtp_password else None,
        )
    else:
        email = ConsoleEmail(settings.email_from)
    key = settings.anthropic_api_key.get_secret_value() if settings.anthropic_api_key else None
    ai = AIClient(db, key, settings.agent_spend_limit_usd)
    return Platform(
        settings=settings, db=db, storage=storage, email=email, ai=ai, jobs=JobQueue(db)
    )


def create_core_app(
    settings: Settings, *, title: str | None = None, csrf_exempt: tuple[str, ...] = ()
) -> FastAPI:
    app = FastAPI(
        title=title or settings.site_name,
        docs_url="/_rb/docs" if not settings.is_prod else None,
        redoc_url=None,
        openapi_url="/_rb/openapi.json",
    )
    app.state.rb = build_platform(settings)
    # Order matters: headers wrap everything, CSRF runs before routes.
    app.add_middleware(
        CSRFMiddleware,
        secure=settings.secure_cookies,
        exempt_prefixes=CSRF_EXEMPT + csrf_exempt,
        max_body=settings.max_upload_bytes + 1024 * 1024,
    )
    app.add_middleware(SecurityHeadersMiddleware, hsts=settings.secure_cookies)

    @app.get("/_rb/health", include_in_schema=False)
    def health() -> dict:
        return {"status": "ok"}

    return app
