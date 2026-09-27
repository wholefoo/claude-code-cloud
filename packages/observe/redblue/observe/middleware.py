"""Pure-ASGI middleware: request ids, timing, per-route metrics and error capture."""

from __future__ import annotations

import logging
import random
import re
import time
import uuid
from collections.abc import Callable, Iterable
from typing import Any

from starlette.concurrency import run_in_threadpool
from starlette.datastructures import MutableHeaders
from starlette.routing import Mount
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from redblue.core.db import Database
from redblue.observe.logging import request_id_var
from redblue.observe.store import capture_exception, record_request

log = logging.getLogger("redblue.observe")

REQUEST_ID_HEADER = "X-Request-ID"
# Incoming ids are echoed into headers and logs, so only a conservative charset is accepted.
SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$")
DEFAULT_SKIP_PREFIXES = ("/static/", "/media/", "/_rb/beacon", "/favicon.ico")
UNMATCHED = "unmatched"


def new_request_id() -> str:
    return uuid.uuid4().hex


def accept_request_id(value: str | None) -> str | None:
    """Return ``value`` if it is a safe request id, else None."""
    if value and SAFE_REQUEST_ID.fullmatch(value):
        return value
    return None


def route_template(scope: Scope, original_root_path: str = "") -> str:
    """The matched route's path template (e.g. ``/posts/{slug}``), or ``"unmatched"``."""
    route = scope.get("route")
    if route is None:
        return UNMATCHED
    root = scope.get("root_path", "") or ""
    prefix = root[len(original_root_path) :] if root.startswith(original_root_path) else ""
    if isinstance(route, Mount):
        # A mounted app handled the request (root_path already includes the mount path).
        return (prefix or route.path) + "/{path}"
    path = getattr(route, "path", None)
    if not isinstance(path, str):
        return UNMATCHED
    return prefix + path


class ObservabilityMiddleware:
    """Assigns ``X-Request-ID``, times requests, records metrics and captures errors.

    Recording never breaks a request: storage failures are logged and swallowed, and
    application exceptions are always re-raised after capture.
    """

    def __init__(
        self,
        app: ASGIApp,
        db: Database,
        *,
        sample_rate: float = 1.0,
        skip_prefixes: Iterable[str] = DEFAULT_SKIP_PREFIXES,
        random_fn: Callable[[], float] = random.random,
    ):
        self.app = app
        self.db = db
        self.sample_rate = max(0.0, min(1.0, sample_rate))
        self.skip_prefixes = tuple(skip_prefixes)
        self._random = random_fn

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        incoming = None
        for k, v in scope.get("headers", []):
            if k == b"x-request-id":
                incoming = v.decode("latin-1")
                break
        rid = accept_request_id(incoming) or new_request_id()
        scope.setdefault("state", {})["request_id"] = rid
        token = request_id_var.set(rid)
        path = scope.get("path", "")
        skip = path.startswith(self.skip_prefixes)
        original_root = scope.get("root_path", "") or ""
        method = scope.get("method", "GET")
        state: dict[str, Any] = {"status": 500, "route": None}
        started = time.perf_counter()

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                state["status"] = message["status"]
                state["route"] = route_template(scope, original_root)
                MutableHeaders(scope=message)[REQUEST_ID_HEADER] = rid
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception as exc:
            route = state["route"] or route_template(scope, original_root)
            await self._safe(
                capture_exception, self.db, exc, request_id=rid, route=f"{method} {route}"
            )
            if not skip:
                await self._record(method, route, 500, started, rid)
            raise
        else:
            if not skip:
                route = state["route"] or route_template(scope, original_root)
                await self._record(method, route, state["status"], started, rid)
        finally:
            request_id_var.reset(token)

    async def _record(self, method: str, route: str, status: int, started: float, rid: str):
        duration_ms = (time.perf_counter() - started) * 1000
        # Errors are always kept; successful requests are sampled.
        if status < 500 and self.sample_rate < 1.0 and self._random() >= self.sample_rate:
            return
        await self._safe(
            record_request,
            self.db,
            method=method,
            route=route,
            status=status,
            duration_ms=duration_ms,
            request_id=rid,
        )

    @staticmethod
    async def _safe(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
        try:
            await run_in_threadpool(fn, *args, **kwargs)
        except Exception:
            log.warning("observability write failed (%s)", fn.__name__, exc_info=True)
