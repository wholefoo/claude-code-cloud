"""OWASP ZAP baseline scan, pointed only at the preview's loopback URL.

Uses ``zap-baseline.py`` when it is on PATH. Otherwise, when ``REDBLUE_ZAP_IMAGE`` names a
ZAP image and Docker is installed (the gate Action sets this up with ``dast_tools``), the
same script runs from that image on the host network so it can reach the loopback preview."""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from pathlib import Path

from redblue.gate.scanners import ScanContext, ScannerUnavailable, find_tool, run_tool
from redblue.gate.schemas import Finding, Severity

_RISK = {"0": Severity.info, "1": Severity.low, "2": Severity.medium, "3": Severity.high}


def available() -> bool:
    """ZAP can run here: the script is on PATH, or Docker and an image are configured."""
    if find_tool("zap-baseline.py") is not None:
        return True
    return bool(os.environ.get("REDBLUE_ZAP_IMAGE", "").strip()) and bool(shutil.which("docker"))


class ZapScanner:
    name = "zap"
    kind = "dast"

    def run(self, ctx: ScanContext) -> list[Finding]:
        tool = find_tool("zap-baseline.py")
        image = os.environ.get("REDBLUE_ZAP_IMAGE", "").strip()
        docker = shutil.which("docker") if tool is None and image else None
        if tool is None and docker is None:
            raise ScannerUnavailable(
                "zap-baseline.py not on PATH (or set REDBLUE_ZAP_IMAGE and install Docker)"
            )
        if ctx.preview is None or ctx.preview.mode != "subprocess":
            raise ScannerUnavailable(
                "ZAP needs a subprocess preview (set start_command or preview_mode: subprocess)"
            )
        target = ctx.preview.base_url  # loopback-asserted by Preview
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "zap.json"
            if tool is not None:
                cmd = [*tool, "-t", target, "-J", str(out), "-I"]
            else:
                cmd = _docker_command(docker, image, target, out)
            proc = run_tool(cmd, ctx.repo_path, timeout=900)
            if not out.exists():
                tail = (proc.stderr or proc.stdout or "")[-300:].strip()
                raise ScannerUnavailable(f"ZAP produced no report ({proc.returncode}): {tail}")
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


# A registry image reference, never an option: the value is passed to `docker run`.
_IMAGE = re.compile(r"[a-z0-9][a-z0-9._/:-]*(@sha256:[0-9a-f]{64})?")


def _docker_command(docker: str, image: str, target: str, out: Path) -> list[str]:
    if not _IMAGE.fullmatch(image):
        raise ScannerUnavailable(f"REDBLUE_ZAP_IMAGE is not an image reference: {image[:80]!r}")
    # ZAP runs as its own user in the container and writes the report to /zap/wrk.
    out.parent.chmod(0o777)  # noqa: S103 - a private temp dir, removed after the scan
    return [
        docker,
        "run",
        "--rm",
        "--network",
        "host",  # the preview listens on the runner's 127.0.0.1 only
        "-v",
        f"{out.parent}:/zap/wrk:rw",
        image,
        "zap-baseline.py",
        "-t",
        target,
        "-J",
        out.name,
        "-I",
    ]


def _strip(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text or "").strip()


def _path(uri: str) -> str:
    from urllib.parse import urlsplit

    return urlsplit(uri).path or "/"
