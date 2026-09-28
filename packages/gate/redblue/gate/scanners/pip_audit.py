"""pip-audit wrapper: known-vulnerable dependencies.

Audits ``requirements.txt`` when there is one. Otherwise it collects the dependencies
declared in the scanned ``pyproject.toml`` files (minus the repo's own packages) and lets
pip-audit resolve what a fresh install would get. The environment the gate runs in is never
audited: its packages belong to the CI machine, not the project."""

from __future__ import annotations

import json
import re
import tempfile
import tomllib
from pathlib import Path

from redblue.gate.scanners import (
    ScanContext,
    ScannerUnavailable,
    find_tool,
    iter_files,
    run_tool,
)
from redblue.gate.schemas import Finding, Severity

REQUIREMENT_FILES = ("requirements.txt", "requirements.lock", "requirements/prod.txt")


def dependency_file(ctx: ScanContext) -> Path | None:
    for base in {ctx.repo_path / ctx.config.app_dir, ctx.repo_path}:
        for name in REQUIREMENT_FILES:
            p = base / name
            if p.is_file():
                return p
    return None


# A requirement line must start with a package name. pyproject.toml comes from the PR under
# test, so lines that pip would read as options (--index-url, -e, -r) or direct references
# (name @ https://…) are dropped rather than handed to pip.
_REQUIREMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*(\[[A-Za-z0-9._, -]+\])?\s*([<>=!~;(]|$)")


def _normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _name(requirement: str) -> str:
    return _normalize(re.split(r"[\s\[<>=!~;(]", requirement, maxsplit=1)[0])


def project_requirements(ctx: ScanContext) -> dict[str, str]:
    """``{requirement: pyproject path}`` for third-party dependencies of the scanned
    projects. Dependencies on the repo's own packages (e.g. a monorepo) are left out."""
    declared: dict[str, str] = {}
    own: set[str] = set()
    for path in iter_files(ctx, (".toml",)):
        if path.name != "pyproject.toml":
            continue
        try:
            project = tomllib.loads(path.read_text(encoding="utf-8")).get("project") or {}
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
            continue
        if isinstance(project.get("name"), str):
            own.add(_normalize(project["name"]))
        for dep in project.get("dependencies") or []:
            dep = str(dep).strip()
            if dep and "\n" not in dep and "@" not in dep and _REQUIREMENT.match(dep):
                declared.setdefault(dep, ctx.rel(path))
    return {dep: where for dep, where in declared.items() if _name(dep) not in own}


class PipAuditScanner:
    name = "pip_audit"
    kind = "deps"

    def run(self, ctx: ScanContext) -> list[Finding]:
        cmd = find_tool("pip-audit", "pip_audit")
        if cmd is None:
            raise ScannerUnavailable("pip-audit is not installed (pip install pip-audit)")
        req = dependency_file(ctx)
        if req is not None:
            proc = self._audit(cmd, req, ctx)
            origin: dict[str, str] = {}
            default_file = ctx.rel(req)
        else:
            declared = project_requirements(ctx)
            if not declared:
                raise ScannerUnavailable("no requirements.txt or pyproject.toml dependencies")
            origin = {_name(dep): where for dep, where in declared.items()}
            default_file = next(iter(declared.values()))
            with tempfile.TemporaryDirectory() as tmp:
                listing = Path(tmp) / "requirements.txt"
                listing.write_text("\n".join(sorted(declared)) + "\n", encoding="utf-8")
                proc = self._audit(cmd, listing, ctx)
        try:
            data = json.loads(proc.stdout or "")
        except json.JSONDecodeError as exc:
            raise ScannerUnavailable(
                f"pip-audit failed ({proc.returncode}): {(proc.stderr or '')[-400:].strip()}"
            ) from exc
        deps = data.get("dependencies", data if isinstance(data, list) else [])
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
                        # Transitive dependencies are reported against the first pyproject.
                        file=origin.get(_normalize(str(dep.get("name", ""))), default_file),
                        evidence=f"fix versions: {', '.join(fixes) or 'none'}; aliases: "
                        f"{', '.join(v.get('aliases') or [])}",
                        code=f"{dep.get('name')}=={dep.get('version')}",
                    )
                )
        return out

    @staticmethod
    def _audit(cmd: list[str], requirements: Path, ctx: ScanContext):
        return run_tool(
            [*cmd, "-r", str(requirements), "-f", "json", "--progress-spinner", "off"],
            cwd=ctx.repo_path,
            timeout=300,
        )
