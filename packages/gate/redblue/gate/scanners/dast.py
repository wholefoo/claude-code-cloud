"""Built-in dynamic checks against the preview the gate launched itself.

Deliberately conservative: passive checks (security headers, cookie flags, error leakage,
version banners) plus two harmless canary tests (is reflected input HTML-encoded? does a
redirect parameter accept an off-site URL?). Deeper active testing is delegated to proven
tools (OWASP ZAP, Nuclei) via their own scanners. Nothing here accepts a URL: requests go
through ``Preview.client()``, which refuses non-loopback hosts.
"""

from __future__ import annotations

import re
import secrets
from urllib.parse import urlsplit

import httpx
from redblue.gate.scanners import ScanContext, ScannerUnavailable
from redblue.gate.schemas import Finding, Severity

REDIRECT_PARAM = re.compile(
    r"^(next|url|redirect|redirect_to|return|return_to|returnurl|to|dest|"
    r"destination|continue|goto)$",
    re.I,
)
CANARY_HOST = "rb-canary.invalid"
TRACE_SIGNS = (
    "Traceback (most recent call last)",
    'File "/',
    "sqlalchemy.exc.",
    "sqlite3.OperationalError",
    "psycopg.errors",
)

PER_SITE_RULES = {"RB-DAST-CSP", "RB-DAST-NOSNIFF", "RB-DAST-REFERRER", "RB-DAST-FRAMING",
                  "RB-DAST-CSP-WEAK", "RB-DAST-BANNER", "RB-DAST-COOKIE-HTTPONLY",
                  "RB-DAST-COOKIE-SAMESITE"}

HEADER_CHECKS = [
    (
        "content-security-policy",
        "RB-DAST-CSP",
        "Missing Content-Security-Policy",
        "CWE-693",
        Severity.medium,
    ),
    (
        "x-content-type-options",
        "RB-DAST-NOSNIFF",
        "Missing X-Content-Type-Options: nosniff",
        "CWE-693",
        Severity.low,
    ),
    ("referrer-policy", "RB-DAST-REFERRER", "Missing Referrer-Policy", "CWE-200", Severity.low),
]


class DastScanner:
    name = "dast"
    kind = "dast"

    def run(self, ctx: ScanContext) -> list[Finding]:
        if ctx.preview is None:
            raise ScannerUnavailable("no preview running (set `app` in .redblue.yml)")
        client = ctx.preview.client()
        budget = [ctx.config.max_requests]
        checks = ctx.dast_checks
        findings: list[Finding] = []
        seen: set[str] = set()

        def get(path: str, params: dict | None = None) -> httpx.Response | None:
            if budget[0] <= 0:
                return None
            budget[0] -= 1
            try:
                return client.get(path, params=params, follow_redirects=False)
            except httpx.HTTPError:
                return None

        by_rule: dict[str, Finding] = {}

        def add(f: Finding) -> None:
            if f.fingerprint in seen:
                return
            seen.add(f.fingerprint)
            if not f.dynamic and f.rule_id in PER_SITE_RULES:
                # Site-wide header issues: one finding listing the affected pages.
                first = by_rule.get(f.rule_id)
                if first is not None:
                    if len(first.evidence) < 500:
                        first.evidence += f", {f.endpoint}"
                    return
                f.evidence = f"Affected pages: {f.endpoint}"
                by_rule[f.rule_id] = f
            findings.append(f)

        pages, param_endpoints = _targets(ctx)
        for path in pages:
            resp = get(path)
            if resp is None:
                continue
            if checks is None or "headers" in checks:
                for f in _passive(path, resp):
                    add(f)
        for path, params in param_endpoints:
            if checks is None or "reflection" in checks:
                for name in params:
                    f = _reflection(get, path, name)
                    if f:
                        add(f)
            if checks is None or "redirect" in checks:
                for name in params:
                    if REDIRECT_PARAM.match(name):
                        f = _redirect(get, path, name)
                        if f:
                            add(f)
        return findings


def _targets(ctx: ScanContext) -> tuple[list[str], list[tuple[str, list[str]]]]:
    """HTML-ish pages to check passively, and GET endpoints with string query parameters."""
    pages: list[str] = ["/"]
    with_params: list[tuple[str, list[str]]] = []
    spec = ctx.preview.openapi() if ctx.preview else None
    for path, ops in ((spec or {}).get("paths") or {}).items():
        get_op = ops.get("get") if isinstance(ops, dict) else None
        if not get_op or "{" in path:
            continue
        params = [p for p in get_op.get("parameters", []) if p.get("in") == "query"]
        required = [p for p in params if p.get("required")]
        str_params = [
            p["name"] for p in params if (p.get("schema") or {}).get("type", "string") == "string"
        ]
        if not required:
            pages.append(path)
        if str_params:
            with_params.append((path, str_params))
    pages += list(ctx.config.probe_paths)
    return list(dict.fromkeys(pages)), with_params


def _is_html(resp: httpx.Response) -> bool:
    return "text/html" in resp.headers.get("content-type", "")


def _passive(path: str, resp: httpx.Response) -> list[Finding]:
    out: list[Finding] = []
    if _is_html(resp):
        for header, rule, title, cwe, sev in HEADER_CHECKS:
            if header not in resp.headers:
                out.append(_f(rule, title, sev, cwe, path, f"No `{header}` header on {path}."))
        csp = resp.headers.get("content-security-policy", "")
        if "x-frame-options" not in resp.headers and "frame-ancestors" not in csp:
            out.append(
                _f(
                    "RB-DAST-FRAMING",
                    "Page can be framed (clickjacking)",
                    Severity.low,
                    "CWE-1021",
                    path,
                    "No X-Frame-Options or CSP frame-ancestors.",
                )
            )
        if "'unsafe-inline'" in csp and "script-src" in csp:
            out.append(
                _f(
                    "RB-DAST-CSP-WEAK",
                    "CSP allows inline scripts",
                    Severity.low,
                    "CWE-693",
                    path,
                    "script-src contains 'unsafe-inline'.",
                )
            )
    for cookie in resp.headers.get_list("set-cookie"):
        name = cookie.split("=", 1)[0].strip()
        low = cookie.lower()
        if any(k in name.lower() for k in ("session", "auth", "token", "sid")):
            if "httponly" not in low:
                out.append(
                    _f(
                        "RB-DAST-COOKIE-HTTPONLY",
                        f"Session cookie `{name}` lacks HttpOnly",
                        Severity.medium,
                        "CWE-1004",
                        path,
                        cookie.split(";")[0][:40],
                    )
                )
            if "samesite" not in low:
                out.append(
                    _f(
                        "RB-DAST-COOKIE-SAMESITE",
                        f"Cookie `{name}` lacks SameSite",
                        Severity.low,
                        "CWE-1275",
                        path,
                        name,
                    )
                )
    server = resp.headers.get("server", "")
    if re.search(r"\d+\.\d+", server):
        out.append(
            _f(
                "RB-DAST-BANNER",
                "Server version disclosed",
                Severity.info,
                "CWE-200",
                path,
                f"Server: {server[:60]}",
            )
        )
    body = resp.text[:200_000] if resp.content else ""
    if any(s in body for s in TRACE_SIGNS):
        out.append(
            _f(
                "RB-DAST-TRACE",
                "Stack trace or database error exposed",
                Severity.medium,
                "CWE-209",
                path,
                "Response body contains a traceback/DB error.",
            )
        )
    return out


def _reflection(get, path: str, param: str) -> Finding | None:
    """Output-encoding check with an inert canary. Flags raw `<` or `"` echoed into HTML."""
    token = "rbc" + secrets.token_hex(4)
    canary = f"{token}<\">"
    resp = get(path, {param: canary})
    if resp is None or not _is_html(resp):
        return None
    body = resp.text
    if token not in body:
        return None
    if f"{token}<" in body or f'{token}<"' in body:
        return _f(
            "RB-DAST-REFLECTED",
            "Reflected input is not HTML-encoded (XSS risk)",
            Severity.high,
            "CWE-79",
            path,
            f"Query parameter `{param}` is echoed into HTML without encoding `<`.",
            method="GET",
            code=param,
            dynamic=True,
        )
    return None


def _redirect(get, path: str, param: str) -> Finding | None:
    resp = get(path, {param: f"https://{CANARY_HOST}/"})
    if resp is None or resp.status_code not in (301, 302, 303, 307, 308):
        return None
    location = resp.headers.get("location", "")
    host = urlsplit(location).hostname or ""
    if host == CANARY_HOST:
        return _f(
            "RB-DAST-OPEN-REDIRECT",
            "Open redirect",
            Severity.medium,
            "CWE-601",
            path,
            f"`{param}` redirects to any external host.",
            method="GET",
            code=param,
            dynamic=True,
        )
    return None


def _f(
    rule: str,
    title: str,
    sev: Severity,
    cwe: str,
    endpoint: str,
    evidence: str,
    *,
    method: str = "GET",
    code: str = "",
    dynamic: bool = False,
) -> Finding:
    return Finding(
        tool="dast",
        rule_id=rule,
        title=title,
        severity=sev,
        cwe=cwe,
        endpoint=endpoint,
        method=method,
        evidence=evidence,
        code=code or rule,
        confidence="high" if dynamic else "medium",
        dynamic=dynamic,
        description=title,
    )
