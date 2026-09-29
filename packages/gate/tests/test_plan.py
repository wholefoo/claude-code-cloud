import json
import subprocess
from pathlib import Path

import pytest

from redblue.gate.config import parse_config
from redblue.gate.gate import decide
from redblue.gate.red import RedAgent, plan
from redblue.gate.report import markdown
from redblue.gate.scanners import ScanContext
from redblue.gate.scanners import semgrep as sg
from redblue.gate.schemas import ScanReport, Severity

CFG = parse_config({"app": "app:app"})


def test_any_python_change_probes_the_preview():
    result = plan(CFG, ["packages/cms/redblue/cms/models.py"])
    assert {"builtin_sast", "bandit", "semgrep", "dast"} <= set(result.scanners)
    assert result.dast_checks == {"headers", "reflection", "redirect"}
    assert "1 Python file(s) changed → SAST + DAST on the preview" in result.reasons


def test_non_code_changes_skip_dast():
    assert "dast" not in plan(CFG, ["docs/PLAN.md"]).scanners
    templates = plan(CFG, ["admin/templates/page.html"])
    assert "dast" in templates.scanners and templates.dast_checks == {"reflection", "headers"}


def test_unavailable_scanners_are_reported_as_skipped_not_run(tmp_path, monkeypatch):
    monkeypatch.setattr(sg.shutil, "which", lambda _: None)
    (tmp_path / "app.py").write_text("x = 1\n")
    report = ScanReport(repo_path=str(tmp_path))
    RedAgent(CFG, tmp_path).scan(report, only=["builtin_sast", "semgrep"])
    assert report.scanners_run == ["builtin_sast"]
    md = markdown(report, decide(report, Severity.high))
    assert "- Scanners run: builtin_sast\n" in md
    assert "- Skipped `semgrep`: semgrep is not on PATH" in md


def test_semgrep_notes_how_much_it_scanned(tmp_path, monkeypatch):
    partial = [
        "PartialParsing",
        [{"path": "t/a.html", "start": {"line": 1}}, {"path": "t/a.html", "start": {"line": 6}}],
    ]
    out = {
        "results": [],
        "errors": [
            {"type": partial, "level": "warn", "path": "t/a.html"},
            {"type": ["PartialParsing", [{"path": "t/b.html"}]], "level": "warn"},
            {"type": "Timeout", "level": "error", "path": "big.py"},
        ],
        "paths": {"scanned": ["t/a.html", "t/b.html", "big.py"]},
    }
    monkeypatch.setattr(sg.shutil, "which", lambda _: "/usr/bin/semgrep")
    monkeypatch.setattr(
        sg,
        "run_tool",
        lambda cmd, cwd, timeout: subprocess.CompletedProcess(cmd, 0, json.dumps(out), ""),
    )
    ctx = ScanContext(repo_path=tmp_path, config=CFG)
    assert sg.SemgrepScanner().run(ctx) == []
    assert ctx.notes == [
        "semgrep: 3 file(s) scanned, 2 only partly parsed (e.g. template syntax), "
        "1 error(s): some files or rules were not analyzed"
    ]


def test_semgrep_clean_run_note():
    assert sg._coverage({"paths": {"scanned": ["a.py"]}, "errors": []}) == (
        "semgrep: 1 file(s) scanned"
    )


class _Preview:
    mode = "subprocess"
    base_url = "http://127.0.0.1:5555"


def test_zap_runs_from_its_docker_image_against_the_loopback_preview(tmp_path, monkeypatch):
    from redblue.gate.scanners import zap

    seen = {}

    def fake_run(cmd, cwd, timeout):
        seen["cmd"] = cmd
        wrk = Path(cmd[cmd.index("-v") + 1].split(":")[0])
        report = {
            "site": [
                {
                    "alerts": [
                        {
                            "pluginid": "10038",
                            "name": "CSP not set",
                            "riskcode": "2",
                            "cweid": "693",
                            "instances": [{"uri": "http://127.0.0.1:5555/a"}],
                        }
                    ]
                }
            ]
        }
        (wrk / "zap.json").write_text(json.dumps(report))
        return subprocess.CompletedProcess(cmd, 2, "", "")

    image = "zaproxy/zap-stable:2.17.0@sha256:" + "a" * 64
    monkeypatch.setattr(zap, "find_tool", lambda *a: None)
    monkeypatch.setattr(zap.shutil, "which", lambda _: "/usr/bin/docker")
    monkeypatch.setenv("REDBLUE_ZAP_IMAGE", image)
    monkeypatch.setattr(zap, "run_tool", fake_run)
    ctx = ScanContext(repo_path=tmp_path, config=CFG, preview=_Preview())
    (finding,) = zap.ZapScanner().run(ctx)

    cmd = seen["cmd"]
    assert cmd[:5] == ["/usr/bin/docker", "run", "--rm", "--network", "host"]
    assert cmd[cmd.index(image) + 1 :][:3] == ["zap-baseline.py", "-t", "http://127.0.0.1:5555"]
    assert finding.endpoint == "/a" and finding.severity.value == "medium"


def test_zap_refuses_an_image_value_that_is_an_option(tmp_path, monkeypatch):
    from redblue.gate.scanners import ScannerUnavailable, zap

    monkeypatch.setattr(zap, "find_tool", lambda *a: None)
    monkeypatch.setattr(zap.shutil, "which", lambda _: "/usr/bin/docker")
    monkeypatch.setenv("REDBLUE_ZAP_IMAGE", "--privileged")
    ctx = ScanContext(repo_path=tmp_path, config=CFG, preview=_Preview())
    with pytest.raises(ScannerUnavailable, match="not an image reference"):
        zap.ZapScanner().run(ctx)


def test_preview_runs_as_a_server_only_when_zap_or_nuclei_can_run(monkeypatch):
    from redblue.gate import gate

    monkeypatch.setattr(gate.zap, "available", lambda: False)
    monkeypatch.setattr(gate.nuclei, "available", lambda: False)
    assert not gate._wants_server(CFG)
    monkeypatch.setattr(gate.nuclei, "available", lambda: True)
    assert gate._wants_server(CFG)
    off = parse_config({"app": "app:app", "scanners": {"nuclei": False}})
    assert not gate._wants_server(off)
    pinned = parse_config({"app": "app:app", "preview_mode": "inprocess"})
    assert not gate._wants_server(pinned)


def test_a_server_that_will_not_start_falls_back_to_in_process(monkeypatch):
    from contextlib import ExitStack, nullcontext

    from redblue.gate import gate

    def fake_preview(config, repo, mode=None):
        if mode == "subprocess":
            raise gate.PreviewError("port in use")
        return nullcontext("in-process preview")

    monkeypatch.setattr(gate, "_wants_server", lambda config: True)
    monkeypatch.setattr(gate, "Preview", fake_preview)
    notes: list[str] = []
    with ExitStack() as stack:
        assert gate._open_preview(stack, CFG, Path("."), notes) == "in-process preview"
    assert notes == ["Preview server failed, using in-process preview (no ZAP/Nuclei): port in use"]
