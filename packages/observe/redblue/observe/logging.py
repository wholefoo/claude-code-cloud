"""Structured (JSON) logging with request correlation.

The middleware stores the current request id in a context variable; every log record
emitted while handling that request carries it, including records from other libraries.
"""

from __future__ import annotations

import json as _json
import logging
import sys
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

request_id_var: ContextVar[str | None] = ContextVar("rb_request_id", default=None)

# Attributes every LogRecord has; anything else was passed via ``extra=`` and is emitted.
_STANDARD_ATTRS = set(vars(logging.LogRecord("x", 0, "x", 0, "x", None, None))) | {
    "message",
    "asctime",
    "request_id",
}
_HANDLER_MARK = "_rb_observe_handler"


def get_request_id() -> str | None:
    return request_id_var.get()


class RequestIdFilter(logging.Filter):
    """Adds ``record.request_id`` (or ``"-"``) so text formats can use ``%(request_id)s``."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not getattr(record, "request_id", None):
            record.request_id = request_id_var.get() or "-"
        return True


class JSONFormatter(logging.Formatter):
    """One JSON object per line: ts, level, logger, msg, request_id, extras, exc."""

    def format(self, record: logging.LogRecord) -> str:
        data: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        rid = getattr(record, "request_id", None) or request_id_var.get()
        if rid and rid != "-":
            data["request_id"] = rid
        for key, value in record.__dict__.items():
            if key not in _STANDARD_ATTRS and not key.startswith("_"):
                data[key] = value
        if record.exc_info:
            data["exc"] = self.formatException(record.exc_info)
        if record.stack_info:
            data["stack"] = self.formatStack(record.stack_info)
        return _json.dumps(data, default=str, ensure_ascii=False)


TEXT_FORMAT = "%(asctime)s %(levelname)s %(name)s [%(request_id)s] %(message)s"


def configure_logging(level: int | str = "INFO", json: bool = True, stream=None) -> logging.Handler:
    """Install one root handler (idempotent: replaces a handler installed earlier by this)."""
    root = logging.getLogger()
    for h in list(root.handlers):
        if getattr(h, _HANDLER_MARK, False):
            root.removeHandler(h)
    handler = logging.StreamHandler(stream or sys.stderr)
    setattr(handler, _HANDLER_MARK, True)
    handler.addFilter(RequestIdFilter())
    handler.setFormatter(JSONFormatter() if json else logging.Formatter(TEXT_FORMAT))
    root.addHandler(handler)
    root.setLevel(level if isinstance(level, int) else level.upper())
    return handler
