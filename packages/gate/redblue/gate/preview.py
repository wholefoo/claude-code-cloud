"""Previews: the only thing the Red agent is allowed to probe.

A :class:`Preview` launches the app under test *itself*, either in-process (ASGI app wrapped
in a test client) or as a local ``uvicorn`` subprocess bound to 127.0.0.1 on a free port.
There is no way to construct a Preview from a URL, and :attr:`Preview.base_url` asserts that
the host is loopback every time it is read.
"""

from __future__ import annotations

import importlib
import os
import secrets
import shlex
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx

from redblue.gate.config import GateConfig

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "testserver", "::1", "[::1]"})
OPENAPI_PATHS = ("/openapi.json", "/_rb/openapi.json")


class PreviewError(RuntimeError):
    pass


class NonLoopbackTarget(PreviewError):
    """Raised whenever something tries to point the Red agent at a non-loopback host."""


def assert_loopback(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    if host not in LOOPBACK_HOSTS:
        raise NonLoopbackTarget(
            f"Refusing to probe {url!r}: the Red agent only scans previews it launched on "
            "127.0.0.1/localhost."
        )
    return url


class _LoopbackGuard(httpx.BaseTransport):
    """Transport wrapper that refuses any request to a non-loopback host (defence in depth,
    e.g. if a client follows a redirect to an external site)."""

    def __init__(self, inner: httpx.BaseTransport):
        self.inner = inner

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        assert_loopback(str(request.url))
        return self.inner.handle_request(request)

    def close(self) -> None:
        self.inner.close()


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Preview:
    """Context manager that launches the configured app and exposes a loopback client.

    >>> with Preview(config, repo_path) as p:
    ...     r = p.client().get("/")
    """

    def __init__(
        self,
        config: GateConfig,
        repo_path: str | Path = ".",
        *,
        mode: Literal["auto", "inprocess", "subprocess"] | None = None,
        startup_timeout: float = 30.0,
        extra_env: dict[str, str] | None = None,
    ):
        self.config = config
        self.repo_path = Path(repo_path).resolve()
        self.app_dir = (self.repo_path / config.app_dir).resolve()
        requested = mode or config.preview_mode
        if requested == "auto":
            requested = "subprocess" if config.start_command else "inprocess"
        if requested == "inprocess" and not config.app:
            raise PreviewError("In-process preview needs `app: module:attr` in .redblue.yml")
        if requested == "subprocess" and not (config.app or config.start_command):
            raise PreviewError("Subprocess preview needs `app` or `start_command`")
        self.mode: Literal["inprocess", "subprocess"] = requested  # type: ignore[assignment]
        self.startup_timeout = startup_timeout
        self.extra_env = dict(extra_env or {})
        self._base_url: str | None = None
        self._client: httpx.Client | None = None
        self._proc: subprocess.Popen | None = None
        self._log: Any = None
        self._tmp: tempfile.TemporaryDirectory | None = None
        self._saved_env: dict[str, str | None] = {}
        self._saved_modules: set[str] = set()
        self._saved_path: list[str] = []
        self._testclient_cm: Any = None
        self._openapi: dict | None = None
        self._openapi_loaded = False
        self.env: dict[str, str] = {}

    # ------------------------------------------------------------------ properties

    @property
    def base_url(self) -> str:
        if self._base_url is None:
            raise PreviewError("Preview is not running")
        return assert_loopback(self._base_url)

    def _set_base_url(self, url: str) -> None:
        self._base_url = assert_loopback(url)

    def client(self) -> httpx.Client:
        if self._client is None:
            raise PreviewError("Preview is not running")
        assert_loopback(str(self._client.base_url))
        return self._client

    # ------------------------------------------------------------------ lifecycle

    def _throwaway_env(self) -> dict[str, str]:
        if self._tmp is None:
            raise PreviewError("Preview temp directory missing")
        tmp = Path(self._tmp.name)
        env = {
            "RB_ENV": "test",
            "RB_DATABASE_URL": f"sqlite:///{tmp / 'preview.db'}",
            "RB_MEDIA_DIR": str(tmp / "media"),
            "RB_STORAGE_DIR": str(tmp / "media"),
            "RB_UPLOAD_DIR": str(tmp / "uploads"),
            "RB_SECRET_KEY": secrets.token_urlsafe(48),  # fresh per preview
            "RB_PREVIEW": "1",
        }
        env.update(self.extra_env)
        return env

    def _child_env(self) -> dict[str, str]:
        env = {
            k: v for k, v in os.environ.items() if k not in {"ANTHROPIC_API_KEY", "GITHUB_TOKEN"}
        }
        env.update(self.env)
        env["PYTHONPATH"] = os.pathsep.join(
            [str(self.app_dir)] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])
        )
        return env

    def _run_seed(self) -> None:
        if not self.config.seed_command:
            return
        proc = subprocess.run(  # noqa: S603 - command comes from the repo owner's config
            shlex.split(self.config.seed_command),
            cwd=self.app_dir,
            env=self._child_env(),
            capture_output=True,
            text=True,
            timeout=300,
        )
        if proc.returncode != 0:
            raise PreviewError(
                f"seed_command failed ({proc.returncode}):\n{(proc.stderr or proc.stdout)[-2000:]}"
            )

    def __enter__(self) -> Preview:
        self._tmp = tempfile.TemporaryDirectory(prefix="redblue-preview-")
        self.env = self._throwaway_env()
        try:
            self._run_seed()
            if self.mode == "inprocess":
                self._start_inprocess()
            else:
                self._start_subprocess()
        except BaseException:
            self.close()
            raise
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _start_inprocess(self) -> None:
        for key, value in self.env.items():
            self._saved_env[key] = os.environ.get(key)
            os.environ[key] = value
        self._saved_path = list(sys.path)
        sys.path.insert(0, str(self.app_dir))
        self._saved_modules = set(sys.modules)
        module_name, _, attr = (self.config.app or "").partition(":")
        # Always import a fresh copy so each preview gets its own throwaway state.
        for name in [m for m in sys.modules if m == module_name or m.startswith(module_name + ".")]:
            del sys.modules[name]
        try:
            module = importlib.import_module(module_name)
            app = module
            for part in attr.split("."):
                app = getattr(app, part)
        except Exception as exc:
            raise PreviewError(f"Could not import ASGI app {self.config.app!r}: {exc}") from exc
        if (
            callable(app)
            and not hasattr(app, "router")
            and getattr(app, "__name__", "")
            in {
                "create_app",
                "make_app",
            }
        ):
            app = app()
        self._client = self._make_inprocess_client(app)
        self._set_base_url(str(self._client.base_url))

    def _make_inprocess_client(self, app: Any) -> httpx.Client:
        try:
            from starlette.testclient import TestClient
        except ImportError:  # pragma: no cover - fastapi apps always have starlette
            return httpx.Client(
                transport=_LoopbackGuard(_SyncASGITransport(app)),
                base_url="http://testserver",
                follow_redirects=False,
                timeout=15,
            )
        client = TestClient(
            app, base_url="http://testserver", raise_server_exceptions=False, follow_redirects=False
        )
        client._transport = _LoopbackGuard(client._transport)  # type: ignore[attr-defined]
        self._testclient_cm = client
        client.__enter__()  # runs lifespan startup
        return client

    def _start_subprocess(self) -> None:
        port = free_port()
        if self.config.start_command:
            cmd = shlex.split(self.config.start_command.format(port=port))
        else:
            cmd = [
                sys.executable,
                "-m",
                "uvicorn",
                self.config.app or "",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--no-access-log",
            ]
        base = f"http://127.0.0.1:{port}"
        self._set_base_url(base)
        # Output goes to a file: a pipe nobody reads fills up during a long scan (ZAP,
        # Nuclei) and blocks the server.
        if self._tmp is None:
            raise PreviewError("Preview temp directory missing")
        self._log = (Path(self._tmp.name) / "server.log").open("w+", errors="replace")
        self._proc = subprocess.Popen(  # noqa: S603 - command from the repo owner's config
            cmd,
            cwd=self.app_dir,
            env=self._child_env(),
            stdout=self._log,
            stderr=subprocess.STDOUT,
            text=True,
        )
        self._client = httpx.Client(
            base_url=base,
            follow_redirects=False,
            timeout=15,
            trust_env=False,  # never route preview traffic through a proxy
            transport=_LoopbackGuard(httpx.HTTPTransport(retries=0)),
        )
        deadline = time.monotonic() + self.startup_timeout
        while time.monotonic() < deadline:
            if self._proc.poll() is not None:
                self._log.seek(0)
                out = self._log.read()
                raise PreviewError(
                    f"Preview exited early ({self._proc.returncode}):\n{out[-2000:]}"
                )
            try:
                self._client.get("/", timeout=2)
                return
            except httpx.TransportError:
                time.sleep(0.2)
        raise PreviewError(f"Preview did not become ready within {self.startup_timeout:.0f}s")

    def close(self) -> None:
        if self._testclient_cm is not None:
            try:
                self._testclient_cm.__exit__(None, None, None)
            except Exception:  # noqa: S110 - best-effort shutdown
                pass
            self._testclient_cm = None
        elif self._client is not None:
            self._client.close()
        self._client = None
        if self._proc is not None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._proc.kill()
            self._proc = None
        if self._log is not None:
            self._log.close()
            self._log = None
        for key, old in self._saved_env.items():
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old
        self._saved_env = {}
        if self._saved_path:
            sys.path[:] = self._saved_path
            self._saved_path = []
        if self._saved_modules:
            for name in set(sys.modules) - self._saved_modules:
                del sys.modules[name]
            self._saved_modules = set()
        if self._tmp is not None:
            self._tmp.cleanup()
            self._tmp = None

    # ------------------------------------------------------------------ helpers

    def openapi(self) -> dict | None:
        if self._openapi_loaded:
            return self._openapi
        self._openapi_loaded = True
        for path in OPENAPI_PATHS:
            try:
                r = self.client().get(path)
            except httpx.HTTPError:
                continue
            if r.status_code == 200:
                try:
                    data = r.json()
                except ValueError:
                    continue
                if isinstance(data, dict) and "paths" in data:
                    self._openapi = data
                    break
        return self._openapi

    def info(self) -> dict:
        spec = self.openapi() or {}
        return {
            "mode": self.mode,
            "base_url": self.base_url,
            "endpoints": len(spec.get("paths", {})),
        }


class _SyncASGITransport(httpx.BaseTransport):  # pragma: no cover - fallback without starlette
    """Run httpx's async ASGITransport from sync code."""

    def __init__(self, app: Any):
        self._inner = httpx.ASGITransport(app=app)

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        import asyncio

        async def _go() -> httpx.Response:
            body = request.read()
            areq = httpx.Request(request.method, request.url, headers=request.headers, content=body)
            resp = await self._inner.handle_async_request(areq)
            content = await resp.aread()
            return httpx.Response(resp.status_code, headers=resp.headers, content=content)

        return asyncio.run(_go())
