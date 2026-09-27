"""Semgrep wrapper. Runs only when ``semgrep`` is on PATH; skipped gracefully otherwise."""

from __future__ import annotations

import json
import re
import shutil

from redblue.gate.scanners import ScanContext, ScannerUnavailable, run_tool
from redblue.gate.schemas import Finding, Severity

SEVERITY = {"ERROR": Severity.high, "WARNING": Severity.medium, "INFO": Severity.low}
CONFIGS = ("p/python", "p/owasp-top-ten")


class SemgrepScanner:
    name = "semgrep"
    kind = "sast"

    def run(self, ctx: ScanContext) -> list[Finding]:
        exe = shutil.which("semgrep")
        if exe is None:
            raise ScannerUnavailable("semgrep is not on PATH")
        cmd = [exe, "--json", "--quiet", "--metrics=off"]
        for cfg in CONFIGS:
            cmd += ["--config", cfg]
        for pattern in ctx.config.all_excludes:
            cmd += ["--exclude", pattern]
        cmd += list(ctx.config.paths)
        proc = run_tool(cmd, cwd=ctx.repo_path, timeout=900)
        try:
            data = json.loads(proc.stdout or "{}")
        except json.JSONDecodeError as exc:
            raise ScannerUnavailable(
                f"semgrep failed ({proc.returncode}): {(proc.stderr or '')[-300:]}"
            ) from exc
        if proc.returncode not in (0, 1) and not data.get("results"):
            raise ScannerUnavailable(f"semgrep failed: {(proc.stderr or '')[-300:]}")
        out = []
        for r in data.get("results", []):
            extra = r.get("extra", {})
            meta = extra.get("metadata", {}) or {}
            cwe = meta.get("cwe")
            if isinstance(cwe, list):
                cwe = cwe[0] if cwe else None
            m = re.search(r"CWE-(\d+)", str(cwe or ""))
            out.append(
                Finding(
                    tool="semgrep",
                    rule_id=r.get("check_id", "semgrep"),
                    title=(extra.get("message") or r.get("check_id", ""))[:200],
                    description=extra.get("message", ""),
                    severity=SEVERITY.get(extra.get("severity", "INFO"), Severity.low),
                    confidence=str(meta.get("confidence", "medium")).lower()  # type: ignore[arg-type]
                    if str(meta.get("confidence", "")).lower() in {"low", "medium", "high"}
                    else "medium",
                    cwe=f"CWE-{m.group(1)}" if m else None,
                    file=ctx.rel(ctx.repo_path / r.get("path", "")),
                    line=(r.get("start") or {}).get("line"),
                    evidence=(extra.get("lines") or "")[:500],
                    code=(extra.get("lines") or "").strip(),
                )
            )
        return out
