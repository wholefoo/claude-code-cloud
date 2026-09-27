"""Secure defaults: security headers + CSP, CSRF, password hashing, safe URLs, rate limits.

These are wired in by :func:`redblue.core.app.create_core_app`; generated apps get them
without opting in.
"""

from __future__ import annotations

import hmac
import secrets
import threading
import time
from collections import defaultdict, deque
from collections.abc import Iterable
from urllib.parse import urlsplit

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import HTTPException, Request
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

# ---------------------------------------------------------------- passwords

_hasher = PasswordHasher()


def hash_password(password: str) -> str:
    if len(password) < 10:
        raise ValueError("Passwords must be at least 10 characters.")
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerificationError, InvalidHashError):
        return False


# ---------------------------------------------------------------- URLs

SAFE_SCHEMES = {"http", "https", "mailto", "tel"}


def is_safe_link(url: str) -> bool:
    """True for relative links and http(s)/mailto/tel URLs. Blocks javascript:, data:, etc."""
    url = url.strip()
    if not url:
        return False
    if any(ord(c) < 0x20 for c in url):
        return False
    if url.startswith("//"):
        return False  # protocol-relative: treated as external+ambiguous
    parts = urlsplit(url)
    if not parts.scheme:
        return True
    return parts.scheme.lower() in SAFE_SCHEMES


def safe_redirect_target(target: str | None, default: str = "/") -> str:
    """Only allow same-site relative redirects (prevents open redirects)."""
    if not target:
        return default
    target = target.strip()
    if not target.startswith("/") or target.startswith("//") or target.startswith("/\\"):
        return default
    if any(ord(c) < 0x20 for c in target) or "\\" in target:
        return default
    return target


# ---------------------------------------------------------------- headers / CSP

DEFAULT_FRAME_SOURCES = (
    "https://www.youtube-nocookie.com",
    "https://player.vimeo.com",
)


def build_csp(nonce: str, frame_sources: Iterable[str] = DEFAULT_FRAME_SOURCES) -> str:
    frames = " ".join(frame_sources) or "'none'"
    return "; ".join(
        [
            "default-src 'self'",
            f"script-src 'self' 'nonce-{nonce}'",
            "style-src 'self'",
            "img-src 'self' data: https:",
            "font-src 'self'",
            "connect-src 'self'",
            f"frame-src {frames}",
            "object-src 'none'",
            "base-uri 'self'",
            "form-action 'self'",
            "frame-ancestors 'none'",
            "upgrade-insecure-requests",
        ]
    )


class SecurityHeadersMiddleware:
    """Adds strict security headers and a per-request CSP nonce (``request.state.csp_nonce``)."""

    def __init__(self, app: ASGIApp, hsts: bool = False, frame_sources=DEFAULT_FRAME_SOURCES):
        self.app = app
        self.hsts = hsts
        self.frame_sources = tuple(frame_sources)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        nonce = secrets.token_urlsafe(16)
        scope.setdefault("state", {})["csp_nonce"] = nonce

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers.setdefault("Content-Security-Policy", build_csp(nonce, self.frame_sources))
                headers.setdefault("X-Content-Type-Options", "nosniff")
                headers.setdefault("X-Frame-Options", "DENY")
                headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
                headers.setdefault(
                    "Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=()"
                )
                headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
                if self.hsts:
                    headers.setdefault(
                        "Strict-Transport-Security", "max-age=63072000; includeSubDomains"
                    )
            await send(message)

        await self.app(scope, receive, send_wrapper)


class HeadAsGetMiddleware:
    """Answer HEAD like GET without a body (crawlers and uptime monitors use HEAD)."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] != "HEAD":
            await self.app(scope, receive, send)
            return
        scope = dict(scope, method="GET")

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.body":
                message = {**message, "body": b""}
            await send(message)

        await self.app(scope, receive, send_wrapper)


# ---------------------------------------------------------------- CSRF

CSRF_COOKIE = "rb_csrf"
CSRF_HEADER = "x-csrf-token"
CSRF_FIELD = "csrf_token"
UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


class CSRFMiddleware:
    """Double-submit CSRF protection for cookie-authenticated requests.

    Every unsafe request must carry the token from the ``rb_csrf`` cookie either in the
    ``X-CSRF-Token`` header (HTMX sends it automatically) or a ``csrf_token`` form field.
    Requests authenticated purely by an ``Authorization`` header (API keys) and explicitly
    exempted paths (signed webhooks, the analytics beacon) are skipped.
    """

    def __init__(
        self,
        app: ASGIApp,
        secure: bool = False,
        exempt_prefixes: Iterable[str] = (),
        max_body: int = 12 * 1024 * 1024,
    ):
        self.app = app
        self.secure = secure
        self.exempt = tuple(exempt_prefixes)
        self.max_body = max_body

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        cookie_token = request.cookies.get(CSRF_COOKIE)
        token = cookie_token or secrets.token_urlsafe(32)
        scope.setdefault("state", {})["csrf_token"] = token

        if request.method in UNSAFE_METHODS and not self._is_exempt(request):
            body = b""
            more = True
            while more:
                message = await receive()
                body += message.get("body", b"")
                more = message.get("more_body", False)
                if len(body) > self.max_body:
                    await _plain(send, 413, b"Request body too large")
                    return
            sent = request.headers.get(CSRF_HEADER)
            if not sent:
                sent = await _form_token(scope, body)
            if not cookie_token or not sent or not hmac.compare_digest(sent, cookie_token):
                await _plain(send, 403, b"CSRF token missing or invalid")
                return
            receive = _replay(body)

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start" and not cookie_token:
                headers = MutableHeaders(scope=message)
                flags = "Path=/; HttpOnly; SameSite=Lax" + ("; Secure" if self.secure else "")
                headers.append("set-cookie", f"{CSRF_COOKIE}={token}; {flags}")
            await send(message)

        await self.app(scope, receive, send_wrapper)

    def _is_exempt(self, request: Request) -> bool:
        if any(request.url.path.startswith(p) for p in self.exempt):
            return True
        auth = request.headers.get("authorization", "")
        return auth.lower().startswith("bearer ") and "rb_session" not in request.cookies


def _replay(body: bytes) -> Receive:
    sent = False

    async def receive() -> Message:
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    return receive


async def _form_token(scope: Scope, body: bytes) -> str | None:
    ctype = dict(scope.get("headers", [])).get(b"content-type", b"").decode("latin-1")
    if not (
        ctype.startswith("application/x-www-form-urlencoded")
        or ctype.startswith("multipart/form-data")
    ):
        return None
    req = Request(dict(scope), _replay(body))
    try:
        form = await req.form()
    except Exception:
        return None
    value = form.get(CSRF_FIELD)
    await form.close()
    return value if isinstance(value, str) else None


async def _plain(send: Send, status: int, body: bytes) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [(b"content-type", b"text/plain; charset=utf-8")],
        }
    )
    await send({"type": "http.response.body", "body": body})


# ---------------------------------------------------------------- rate limiting


class RateLimiter:
    """In-process sliding-window limiter. Good for single-node deployments; swap for a
    shared store when running several app instances."""

    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def hit(self, key: str, limit: int, window: float) -> bool:
        now = time.monotonic()
        with self._lock:
            q = self._hits[key]
            while q and q[0] <= now - window:
                q.popleft()
            if len(q) >= limit:
                return False
            q.append(now)
            return True

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


limiter = RateLimiter()


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def rate_limit(bucket: str, limit: int, window: float = 60.0):
    """FastAPI dependency: ``Depends(rate_limit("login", 5, 60))``."""

    def dependency(request: Request) -> None:
        if not limiter.hit(f"{bucket}:{client_ip(request)}", limit, window):
            raise HTTPException(status_code=429, detail="Too many requests, slow down.")

    return dependency
