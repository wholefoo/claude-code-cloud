"""Assemble the full platform: core + observability + CMS API + growth + admin + site.

Run with ``uvicorn redblue.platform.app:app`` or ``redblue dev``. Generated sites call
:func:`create_app` from their own ``main.py`` and can pass extra template directories to
override any page type.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI

from redblue.core.app import create_core_app
from redblue.core.config import Settings, get_settings


def create_app(
    settings: Settings | None = None,
    *,
    template_dirs: list[Path] | None = None,
    observe: bool = True,
) -> FastAPI:
    settings = settings or get_settings()
    app = create_core_app(settings)
    if observe:
        from redblue.observe import install as install_observe

        install_observe(
            app, configure_logs=settings.env != "test", uptime_paths=("/", "/_rb/health")
        )
    from redblue.admin.app import install_admin
    from redblue.cms.api import cms_api_router
    from redblue.growth.routes import install_growth
    from redblue.templates.site import install_site

    app.include_router(cms_api_router())
    install_growth(app)
    install_admin(app)
    try:  # optional: redblue-video
        from redblue.video.admin import install_video
    except ImportError:
        pass
    else:
        install_video(app)
    # The public site owns the catch-all route, so it is installed last.
    install_site(app, extra_template_dirs=template_dirs)
    return app


def __getattr__(name: str):
    # `uvicorn redblue.platform.app:app` builds the app lazily from environment settings.
    if name == "app":
        global app
        app = create_app()
        return app
    raise AttributeError(name)
