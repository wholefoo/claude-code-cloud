"""Report renderers: Markdown (PR comment / job summary), SARIF 2.1.0, JSON."""

from __future__ import annotations

import json

from redblue.gate.schemas import FindingStatus, GateDecision, ScanReport, Severity

_ICON = {"critical": "🟥", "high": "🟧", "medium": "🟨", "low": "🟦", "info": "⬜"}
_SARIF_LEVEL = {
    "critical": "error",
    "high": "error",
    "medium": "warning",
    "low": "note",
    "info": "note",
}


def _md_escape(text: str) -> str:
    return (text or "").replace("|", "\\|").replace("\n", " ").replace("<", "&lt;")[:300]


def markdown(report: ScanReport, decision: GateDecision) -> str:
    open_ = sorted(report.open_findings(), key=lambda f: -f.severity.rank)
    lines = ["## 🔴🔵 RedBlue security gate", "", f"**{decision.summary}**", ""]
    counts = {s.value: 0 for s in Severity}
    for f in open_:
        counts[f.severity.value] += 1
    lines.append(" · ".join(f"{_ICON[k]} {k}: {v}" for k, v in reversed(counts.items())))
    lines.append("")
    if open_:
        lines += ["| Severity | Finding | Location | Tool | CWE |", "|---|---|---|---|---|"]
        for f in open_[:100]:
            lines.append(
                f"| {_ICON[f.severity.value]} {f.severity.value} | "
                f"{_md_escape(f.title)} | `{_md_escape(f.location)}` | {f.tool} | "
                f"{f.cwe or ''} |"
            )
        if len(open_) > 100:
            lines.append(f"\n…and {len(open_) - 100} more (see the SARIF/JSON report).")
    suppressed = [f for f in report.findings if f.status != FindingStatus.open]
    if suppressed:
        lines += ["", f"<details><summary>{len(suppressed)} suppressed finding(s)</summary>", ""]
        lines += [
            f"- `{f.location}` {_md_escape(f.title)}: {f.status.value}"
            f" ({_md_escape(f.triage_note)})"
            for f in suppressed[:50]
        ]
        lines += ["", "</details>"]
    if report.fixes:
        lines += ["", "### Proposed fixes (Blue agent)", ""]
        for p in report.fixes:
            state = "✅ verified" if p.verified else "⚠️ not verified"
            link = f" · [PR]({p.pr_url})" if p.pr_url else ""
            lines.append(
                f"- {state}: {_md_escape(p.summary)} (test `{p.regression_test.path}`){link}"
            )
    lines += ["", "<details><summary>Scan details</summary>", ""]
    lines.append(f"- Scanners run: {', '.join(report.scanners_run) or 'none'}")
    for s in report.scanners_skipped:
        lines.append(f"- Skipped `{s.name}`: {_md_escape(s.reason)}")
    if report.preview:
        lines.append(
            f"- Preview: {report.preview.mode or 'n/a'} "
            f"({report.preview.endpoints} endpoints)"
            + (f"; error: {_md_escape(report.preview.error)}" if report.preview.error else "")
        )
    for n in report.notes:
        lines.append(f"- {_md_escape(n)}")
    lines.append(f"- AI used: {'yes' if report.ai_used else 'no'} (cost ${report.ai_cost_usd:.4f})")
    lines += ["", "</details>"]
    return "\n".join(lines) + "\n"


def sarif(report: ScanReport, *, include_suppressed: bool = False) -> dict:
    """SARIF 2.1.0 for GitHub code scanning.

    Accepted and false-positive findings are left out by default: code scanning does not
    honour SARIF ``suppressions`` and would reopen them as alerts. They remain listed in the
    Markdown and JSON reports with their triage notes.
    """
    rules: dict[str, dict] = {}
    results = []
    for f in report.findings:
        if f.status != FindingStatus.open and not include_suppressed:
            continue
        rules.setdefault(
            f.rule_id,
            {
                "id": f.rule_id,
                "name": f.rule_id,
                "shortDescription": {"text": f.title[:200]},
                "properties": {
                    "tags": ["security"] + ([f.cwe] if f.cwe else []),
                    "security-severity": {
                        "critical": "9.5",
                        "high": "8.0",
                        "medium": "5.5",
                        "low": "3.0",
                        "info": "1.0",
                    }[f.severity.value],
                },
            },
        )
        loc: dict = {}
        if f.file:
            loc = {
                "physicalLocation": {
                    "artifactLocation": {"uri": f.file},
                    "region": {"startLine": f.line or 1},
                }
            }
        else:
            loc = {
                "physicalLocation": {
                    "artifactLocation": {"uri": ".redblue.yml"},
                    "region": {"startLine": 1},
                },
                "logicalLocations": [{"name": f"{f.method or 'GET'} {f.endpoint}"}],
            }
        result = {
            "ruleId": f.rule_id,
            "level": _SARIF_LEVEL[f.severity.value],
            "message": {"text": f"{f.title}. {f.evidence}".strip()[:1000]},
            "locations": [loc],
            "partialFingerprints": {"redblue/v1": f.fingerprint},
        }
        if f.status != FindingStatus.open:
            result["suppressions"] = [
                {"kind": "external", "justification": f.triage_note or f.status.value}
            ]
        results.append(result)
    return {
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "version": "2.1.0",
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": "RedBlue",
                        "informationUri": "https://github.com/wholefoo/redblue",
                        "rules": list(rules.values()),
                    }
                },
                "results": results,
            }
        ],
    }


def as_json(report: ScanReport, decision: GateDecision) -> str:
    return json.dumps(
        {"decision": decision.model_dump(mode="json"), "report": report.model_dump(mode="json")},
        indent=2,
    )
