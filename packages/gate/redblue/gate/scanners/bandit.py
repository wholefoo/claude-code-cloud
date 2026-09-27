"""Bandit (Python SAST) wrapper."""

from __future__ import annotations

import json

from redblue.gate.scanners import ScanContext, ScannerUnavailable, find_tool, iter_files, run_tool
from redblue.gate.schemas import Finding, Severity

SEVERITY = {"LOW": Severity.low, "MEDIUM": Severity.medium, "HIGH": Severity.high}
CONFIDENCE = {"LOW": "low", "MEDIUM": "medium", "HIGH": "high"}


class BanditScanner:
    name = "bandit"
    kind = "sast"

    def run(self, ctx: ScanContext) -> list[Finding]:
        cmd = find_tool("bandit", "bandit")
        if cmd is None:
            raise ScannerUnavailable("bandit is not installed (pip install bandit)")
        files = [str(p) for p in iter_files(ctx, (".py",))]
        if not files:
            return []
        proc = run_tool([*cmd, "-f", "json", "-q", *files], cwd=ctx.repo_path)
        if proc.returncode not in (0, 1):
            raise ScannerUnavailable(f"bandit failed: {(proc.stderr or proc.stdout)[-500:]}")
        try:
            data = json.loads(proc.stdout or "{}")
        except json.JSONDecodeError as exc:
            raise ScannerUnavailable(f"bandit produced invalid JSON: {exc}") from exc
        return [self._finding(ctx, r) for r in data.get("results", [])]

    @staticmethod
    def _finding(ctx: ScanContext, r: dict) -> Finding:
        cwe = (r.get("issue_cwe") or {}).get("id")
        code = r.get("code") or ""
        line = r.get("line_number")
        # Bandit's code excerpt spans several numbered lines; keep only the flagged one.
        flagged = ""
        for raw in code.splitlines():
            num, _, text = raw.partition(" ")
            if num.isdigit() and int(num) == line:
                flagged = text
        return Finding(
            tool="bandit",
            rule_id=r.get("test_id", "B000"),
            title=f"{r.get('test_name', 'bandit')}: {r.get('issue_text', '')}"[:200],
            description=r.get("issue_text", ""),
            severity=SEVERITY.get(r.get("issue_severity", "LOW"), Severity.low),
            confidence=CONFIDENCE.get(r.get("issue_confidence", "MEDIUM"), "medium"),  # type: ignore[arg-type]
            cwe=f"CWE-{cwe}" if cwe else None,
            file=ctx.rel(r.get("filename", "")),
            line=line,
            evidence=code.strip()[:500],
            code=flagged.strip() or code.strip(),
        )
