"""pip-audit wrapper: known-vulnerable dependencies."""

from __future__ import annotations

import json
from pathlib import Path

from redblue.gate.scanners import ScanContext, ScannerUnavailable, find_tool, run_tool
from redblue.gate.schemas import Finding, Severity

REQUIREMENT_FILES = ("requirements.txt", "requirements.lock", "requirements/prod.txt")


def dependency_file(ctx: ScanContext) -> Path | None:
    for base in {ctx.repo_path / ctx.config.app_dir, ctx.repo_path}:
        for name in REQUIREMENT_FILES:
            p = base / name
            if p.is_file():
                return p
    return None


class PipAuditScanner:
    name = "pip_audit"
    kind = "deps"

    def run(self, ctx: ScanContext) -> list[Finding]:
        cmd = find_tool("pip-audit", "pip_audit")
        if cmd is None:
            raise ScannerUnavailable("pip-audit is not installed (pip install pip-audit)")
        req = dependency_file(ctx)
        if req is None:
            raise ScannerUnavailable("no requirements.txt found to audit")
        proc = run_tool(
            [*cmd, "-r", str(req), "-f", "json", "--progress-spinner", "off"],
            cwd=ctx.repo_path,
            timeout=300,
        )
        try:
            data = json.loads(proc.stdout or "")
        except json.JSONDecodeError as exc:
            raise ScannerUnavailable(
                f"pip-audit failed ({proc.returncode}): {(proc.stderr or '')[-400:].strip()}"
            ) from exc
        deps = data.get("dependencies", data if isinstance(data, list) else [])
        rel = ctx.rel(req)
        out: list[Finding] = []
        for dep in deps:
            for v in dep.get("vulns", []) or []:
                fixes = v.get("fix_versions") or []
                out.append(
                    Finding(
                        tool="pip-audit",
                        rule_id=v.get("id", "PYSEC"),
                        title=f"{dep.get('name')} {dep.get('version')} has a known "
                        f"vulnerability ({v.get('id')})",
                        description=(v.get("description") or "")[:2000],
                        # pip-audit has no severity; a CVE with an available fix is actionable.
                        severity=Severity.high if fixes else Severity.medium,
                        confidence="high",
                        cwe="CWE-1395",
                        file=rel,
                        evidence=f"fix versions: {', '.join(fixes) or 'none'}; aliases: "
                        f"{', '.join(v.get('aliases') or [])}",
                        code=f"{dep.get('name')}=={dep.get('version')}",
                    )
                )
        return out
