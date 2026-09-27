"""OWASP ZAP baseline scan, pointed only at the preview's loopback URL."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from redblue.gate.scanners import ScanContext, ScannerUnavailable, find_tool, run_tool
from redblue.gate.schemas import Finding, Severity

_RISK = {"0": Severity.info, "1": Severity.low, "2": Severity.medium, "3": Severity.high}


class ZapScanner:
    name = "zap"
    kind = "dast"

    def run(self, ctx: ScanContext) -> list[Finding]:
        tool = find_tool("zap-baseline.py")
        if tool is None:
            raise ScannerUnavailable("zap-baseline.py not on PATH")
        if ctx.preview is None or ctx.preview.mode != "subprocess":
            raise ScannerUnavailable(
                "ZAP needs a subprocess preview (set start_command or preview_mode: subprocess)"
            )
        target = ctx.preview.base_url  # loopback-asserted by Preview
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "zap.json"
            run_tool([*tool, "-t", target, "-J", str(out), "-I"], ctx.repo_path, timeout=900)
            if not out.exists():
                raise ScannerUnavailable("ZAP produced no report")
            data = json.loads(out.read_text())
        findings = []
        for site in data.get("site", []):
            for alert in site.get("alerts", []):
                inst = (alert.get("instances") or [{}])[0]
                findings.append(
                    Finding(
                        tool="zap",
                        rule_id=f"ZAP-{alert.get('pluginid')}",
                        title=alert.get("name", "ZAP alert"),
                        description=_strip(alert.get("desc", ""))[:1000],
                        severity=_RISK.get(str(alert.get("riskcode")), Severity.low),
                        cwe=f"CWE-{alert['cweid']}"
                        if alert.get("cweid") not in (None, "", "-1")
                        else None,
                        endpoint=_path(inst.get("uri", "")),
                        method=inst.get("method", "GET"),
                        evidence=_strip(inst.get("evidence", ""))[:300],
                        dynamic=True,
                    )
                )
        return findings


def _strip(text: str) -> str:
    import re

    return re.sub(r"<[^>]+>", "", text or "").strip()


def _path(uri: str) -> str:
    from urllib.parse import urlsplit

    return urlsplit(uri).path or "/"
