"""Red agent: plans which scanners and checks to run from the diff and the OpenAPI spec,
runs them against code and the self-launched preview, dedupes, applies ignores, and triages
noise. It orchestrates proven tools; it does not invent exploits."""

from __future__ import annotations

import fnmatch
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field
from redblue.gate.config import GateConfig
from redblue.gate.llm import LLM, untrusted
from redblue.gate.scanners import ScanContext, ScannerUnavailable, all_scanners, is_test_path
from redblue.gate.schemas import Finding, FindingStatus, ScanReport, Severity, SkippedScanner

TRIAGE_SYSTEM = """You are the Red agent's triage step in a defensive security gate. You
receive findings from static and dynamic scanners run against the user's own repository and a
local preview of it. For each finding decide whether it is a true positive, a false positive,
or needs human review, using the code context. Be conservative: only mark false_positive when
the context clearly shows the issue cannot occur (test fixture, constant input, already
escaped). Keep notes to one sentence."""


class TriageItem(BaseModel):
    finding_id: str
    verdict: Literal["true_positive", "false_positive", "needs_review"]
    confidence: Literal["low", "medium", "high"] = "medium"
    note: str = Field(default="", max_length=300)


class TriageResult(BaseModel):
    items: list[TriageItem]


@dataclass
class ScanPlan:
    scanners: list[str]
    dast_checks: set[str] | None
    reasons: list[str] = field(default_factory=list)


def changed_files(repo: Path, base_ref: str | None) -> list[str] | None:
    if not base_ref:
        return None
    try:
        out = subprocess.run(  # noqa: S603 - fixed argv
            ["git", "diff", "--name-only", f"{base_ref}...HEAD"],  # noqa: S607
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    return [line for line in out.stdout.splitlines() if line.strip()]


def plan(config: GateConfig, changed: list[str] | None) -> ScanPlan:
    enabled = [n for n in all_scanners() if config.scanners.enabled(n)]
    if changed is None:
        return ScanPlan(enabled, None, ["full scan (no base ref)"])
    reasons: list[str] = []
    chosen: set[str] = set()
    dast: set[str] = set()
    code = [f for f in changed if f.endswith(".py")]
    templates = [f for f in changed if f.endswith((".html", ".jinja", ".j2"))]
    deps = [
        f
        for f in changed
        if Path(f).name in ("requirements.txt", "pyproject.toml", "poetry.lock", "uv.lock")
    ]
    if code:
        chosen |= {"builtin_sast", "bandit", "semgrep"}
        reasons.append(f"{len(code)} Python file(s) changed → SAST")
        if any(
            "route" in f or "api" in f or "app" in f or "main" in f or "views" in f for f in code
        ):
            chosen |= {"dast", "zap", "nuclei"}
            dast |= {"headers", "reflection", "redirect"}
            reasons.append("routing code changed → DAST")
    if templates:
        chosen |= {"builtin_sast", "dast"}
        dast |= {"reflection", "headers"}
        reasons.append(f"{len(templates)} template(s) changed → template SAST + encoding checks")
    if deps:
        chosen.add("pip_audit")
        reasons.append("dependencies changed → dependency audit")
    if not chosen:
        reasons.append("no security-relevant changes; running baseline SAST")
        chosen.add("builtin_sast")
    return ScanPlan([n for n in enabled if n in chosen], dast or None, reasons)


class RedAgent:
    def __init__(self, config: GateConfig, repo: Path, llm: LLM | None = None):
        self.config, self.repo, self.llm = config, repo, llm

    def scan(
        self,
        report: ScanReport,
        preview=None,
        changed: list[str] | None = None,
        only: list[str] | None = None,
    ) -> ScanReport:
        p = plan(self.config, changed)
        report.notes.extend(p.reasons)
        ctx = ScanContext(self.repo, self.config, preview, changed, p.dast_checks)
        scanners = all_scanners()
        findings: dict[str, Finding] = {}
        for name in only or p.scanners:
            scanner = scanners[name]
            if scanner.kind == "dast" and preview is None:
                report.scanners_skipped.append(SkippedScanner(name=name, reason="no preview"))
                continue
            try:
                results = scanner.run(ctx)
            except ScannerUnavailable as exc:
                report.scanners_skipped.append(SkippedScanner(name=name, reason=exc.reason))
                continue
            report.scanners_run.append(name)
            for f in results:
                findings.setdefault(f.fingerprint, f)
        report.notes.extend(ctx.notes)
        report.findings = self._dedupe_across_tools(list(findings.values()))
        self._apply_ignores(report.findings)
        self.triage(report)
        return report

    @staticmethod
    def _dedupe_across_tools(findings: list[Finding]) -> list[Finding]:
        """Bandit/Semgrep and the built-in rules often flag the same line; keep the built-in
        finding (it carries fixer support) and note the corroborating tools."""
        builtin = {(f.file, f.line): f for f in findings
                   if f.tool == "redblue-sast" and f.file and f.line}
        out: list[Finding] = []
        for f in findings:
            twin = builtin.get((f.file, f.line)) if f.tool in ("bandit", "semgrep") else None
            if twin is not None:
                twin.evidence = (twin.evidence + f" (also reported by {f.tool}: "
                                 f"{f.rule_id})").strip()
                if twin.confidence != "high":
                    twin.confidence = "high"
                continue
            out.append(f)
        return out

    def _apply_ignores(self, findings: list[Finding]) -> None:
        for f in findings:
            for rule in self.config.ignore:
                if _match(rule, f):
                    f.status = FindingStatus.accepted
                    f.triage_note = f"Ignored in config: {rule.reason}"
                    break

    def triage(self, report: ScanReport) -> None:
        # Asserts in test code are expected; drop them entirely rather than list them.
        report.findings = [f for f in report.findings
                           if not (is_test_path(f.file) and f.rule_id == "B101")]
        open_ = [f for f in report.findings if f.is_open]
        for f in open_:  # deterministic noise filters first
            if is_test_path(f.file) and f.rule_id in (
                "RB-SECRET",
                "B105",
                "B106",
                "B101",
                "B311",
                "RB-WEAK-RANDOM",
            ):
                f.status = FindingStatus.false_positive
                f.triage_note = "Test code: not shipped to production."
            elif is_test_path(f.file) and f.severity > Severity.low:
                f.severity = f.severity.lowered()
                f.triage_note = "Lowered: finding is in test code."
        if not (self.llm and self.llm.available):
            return
        candidates = [f for f in report.findings if f.is_open and not f.dynamic][:40]
        if not candidates:
            return
        prompt = "Triage these findings:\n\n" + "\n\n".join(
            untrusted(
                f"finding {f.id}",
                f"id={f.id} rule={f.rule_id} severity={f.severity.value}"
                f" location={f.location}\n{f.title}\n{self._context(f)}",
            )
            for f in candidates
        )
        try:
            result = self.llm.structured(
                model=self.config.model_red,
                system=TRIAGE_SYSTEM,
                prompt=prompt,
                output=TriageResult,
                task="red.triage",
                max_tokens=8000,
            )
        except Exception as exc:  # noqa: BLE001 - triage is best-effort
            report.notes.append(f"AI triage skipped: {exc}")
            return
        report.ai_used = True
        by_id = {f.id: f for f in candidates}
        for item in result.items:
            f = by_id.get(item.finding_id)
            if f is None:
                continue
            f.triage_note = item.note
            if item.verdict == "false_positive" and item.confidence == "high":
                f.status = FindingStatus.false_positive

    def _context(self, f: Finding, radius: int = 6) -> str:
        if not f.file or not f.line:
            return f.evidence
        path = self.repo / f.file
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return f.evidence
        lo, hi = max(0, f.line - radius - 1), min(len(lines), f.line + radius)
        return "\n".join(f"{i + 1:>5}  {lines[i]}" for i in range(lo, hi))


def _match(rule, f: Finding) -> bool:
    fp = getattr(rule, "fingerprint", None)
    rid = getattr(rule, "rule_id", None)
    path = getattr(rule, "path", None)
    if fp and not f.fingerprint.startswith(fp):
        return False
    if rid and rid != f.rule_id:
        return False
    if path and not (fnmatch.fnmatch(f.file or "", path) or (f.file or "").startswith(path)):
        return False
    return bool(fp or rid or path)
