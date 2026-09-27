"""The release gate: scan (Red), optionally fix (Blue), decide."""

from __future__ import annotations

from pathlib import Path

from redblue.gate.blue import BlueAgent
from redblue.gate.config import GateConfig
from redblue.gate.llm import LLM
from redblue.gate.preview import Preview, PreviewError
from redblue.gate.red import RedAgent, changed_files
from redblue.gate.schemas import (
    GateDecision,
    PreviewInfo,
    ScanReport,
    Severity,
    utcnow,
)


def decide(report: ScanReport, threshold: Severity) -> GateDecision:
    blocking = [f for f in report.open_findings() if f.severity >= threshold]
    blocking.sort(key=lambda f: -f.severity.rank)
    if blocking:
        summary = f"❌ Gate failed: {len(blocking)} open finding(s) at or above {threshold.value}."
    else:
        summary = f"✅ Gate passed: no open findings at or above {threshold.value}."
    return GateDecision(
        passed=not blocking, threshold=threshold, blocking=blocking, summary=summary
    )


def run_gate(
    config: GateConfig,
    repo_path: str | Path = ".",
    *,
    base_ref: str | None = None,
    fix: bool = False,
    use_ai: bool = True,
    only: list[str] | None = None,
    threshold: Severity | None = None,
) -> tuple[ScanReport, GateDecision]:
    repo = Path(repo_path).resolve()
    llm = LLM.from_env(spend_limit_usd=config.spend_limit_usd, enabled=use_ai)
    changed = changed_files(repo, base_ref)
    report = ScanReport(repo_path=str(repo), base_ref=base_ref, changed_files=changed)
    red = RedAgent(config, repo, llm)
    preview_needed = config.app is not None or config.start_command is not None
    if preview_needed and any(config.scanners.enabled(n) for n in ("dast", "zap", "nuclei")):
        try:
            with Preview(config, repo) as preview:
                report.preview = PreviewInfo(**preview.info())
                red.scan(report, preview, changed, only)
        except PreviewError as exc:
            report.preview = PreviewInfo(error=str(exc))
            report.notes.append(f"Preview failed, DAST skipped: {exc}")
            red.scan(report, None, changed, only)
    else:
        red.scan(report, None, changed, only)
    thr = threshold or config.severity_threshold
    if fix:
        blue = BlueAgent(config, repo, llm)
        for f in report.open_findings():
            if f.severity < thr:
                continue
            proposal = blue.propose(f)
            if proposal:
                report.fixes.append(blue.verify(proposal))
    report.ai_cost_usd = round(llm.spent_usd, 4)
    report.finished_at = utcnow()
    return report, decide(report, thr)
