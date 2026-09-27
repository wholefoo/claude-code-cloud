"""Optional OpenTelemetry wiring (``pip install redblue-observe[otel]``).

Without the OpenTelemetry packages everything here is a no-op, so jobs and agents can
always wrap work in :func:`span`.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

log = logging.getLogger("redblue.observe")

_tracer = None


def otel_available() -> bool:
    try:
        import opentelemetry.trace  # noqa: F401
    except ImportError:
        return False
    return True


def setup_otel(app: Any, service_name: str, endpoint: str | None = None, *, engine=None) -> bool:
    """Instrument FastAPI (and SQLAlchemy when ``engine`` is given) if OTel is installed.

    Spans are exported over OTLP when ``endpoint`` or ``OTEL_EXPORTER_OTLP_ENDPOINT`` is
    set. Returns True if instrumentation was installed, False (no-op) otherwise.
    """
    global _tracer
    if not otel_available():
        return False
    try:
        from opentelemetry import trace
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:
        return False

    endpoint = endpoint or os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    if endpoint:
        try:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        except ImportError:
            log.warning("OTLP endpoint set but opentelemetry-exporter-otlp is not installed")
        else:
            provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
    try:
        trace.set_tracer_provider(provider)
    except Exception:  # a provider was already set (e.g. by the host process)
        log.debug("tracer provider already configured", exc_info=True)
    _tracer = trace.get_tracer("redblue")

    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        FastAPIInstrumentor.instrument_app(app)
    except ImportError:
        log.info("opentelemetry-instrumentation-fastapi not installed; skipping")
    if engine is not None:
        try:
            from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor

            SQLAlchemyInstrumentor().instrument(engine=engine)
        except ImportError:
            log.info("opentelemetry-instrumentation-sqlalchemy not installed; skipping")
    return True


@contextmanager
def span(name: str, **attrs: Any) -> Iterator[Any]:
    """Trace a unit of work (a job, an agent call). Yields the span, or None without OTel."""
    if _tracer is None:
        yield None
        return
    with _tracer.start_as_current_span(name) as sp:
        for k, v in attrs.items():
            if isinstance(v, str | bool | int | float):
                sp.set_attribute(k, v)
            else:
                sp.set_attribute(k, str(v))
        yield sp
