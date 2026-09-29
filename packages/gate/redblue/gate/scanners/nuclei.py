"""Nuclei with its maintained templates, pointed only at the preview's loopback URL."""

from __future__ import annotations

import json
from urllib.parse import urlsplit

from redblue.gate.scanners import ScanContext, ScannerUnavailable, find_tool, run_tool
from redblue.gate.schemas import Finding, Severity


def available() -> bool:
    return find_tool("nuclei") is not None


class NucleiScanner:
    name = "nuclei"
    kind = "dast"

    def run(self, ctx: ScanContext) -> list[Finding]:
        tool = find_tool("nuclei")
        if tool is None:
            raise ScannerUnavailable("nuclei not on PATH")
        if ctx.preview is None or ctx.preview.mode != "subprocess":
            raise ScannerUnavailable("Nuclei needs a subprocess preview")
        target = ctx.preview.base_url  # loopback-asserted by Preview
        proc = run_tool(
            [
                *tool,
                "-u",
                target,
                "-jsonl",
                "-silent",
                "-disable-update-check",
                "-no-interactsh",  # no out-of-band callbacks to public servers
                "-severity",
                "low,medium,high,critical",
            ],
            ctx.repo_path,
            timeout=900,
        )
        findings = []
        for line in proc.stdout.splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            info = row.get("info", {})
            cwe = ((info.get("classification") or {}).get("cwe-id") or [None])[0]
            findings.append(
                Finding(
                    tool="nuclei",
                    rule_id=row.get("template-id", "nuclei"),
                    title=info.get("name", "Nuclei finding"),
                    description=(info.get("description") or "")[:1000],
                    severity=Severity.parse(info.get("severity", "low")),
                    cwe=cwe.upper() if cwe else None,
                    endpoint=urlsplit(row.get("matched-at", "")).path or "/",
                    evidence=str(row.get("extracted-results") or "")[:300],
                    dynamic=True,
                )
            )
        return findings
