import json
import subprocess

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
    out = {
        "results": [],
        "errors": [{"type": "Syntax error"}],
        "paths": {"scanned": ["a.py", "b.py"]},
    }
    monkeypatch.setattr(sg.shutil, "which", lambda _: "/usr/bin/semgrep")
    monkeypatch.setattr(
        sg,
        "run_tool",
        lambda cmd, cwd, timeout: subprocess.CompletedProcess(cmd, 0, json.dumps(out), ""),
    )
    ctx = ScanContext(repo_path=tmp_path, config=CFG)
    assert sg.SemgrepScanner().run(ctx) == []
    assert ctx.notes == ["semgrep: 2 file(s) scanned, 1 file(s) or rule(s) could not be analyzed"]
