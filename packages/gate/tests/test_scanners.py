from redblue.gate.config import parse_config
from redblue.gate.gate import decide, run_gate
from redblue.gate.scanners.builtin_sast import scan_python_source, scan_template
from redblue.gate.schemas import Finding, Severity


def rules(src):
    return {f.rule_id for f in scan_python_source(src, "app.py")}


def test_builtin_rules():
    assert "RB-SQLI" in rules("def f(db, q):\n    db.execute(f\"SELECT * FROM t WHERE a='{q}'\")\n")
    assert "RB-CMDI" in rules("import subprocess\ndef f(c):\n    subprocess.run(c, shell=True)\n")
    assert "RB-YAML-LOAD" in rules("import yaml\ndef f(s):\n    return yaml.load(s)\n")
    assert "RB-TLS-VERIFY" in rules("import httpx\nhttpx.get('https://x', verify=False)\n")
    assert "RB-EVAL" in rules("def f(s):\n    return eval(s)\n")
    assert "RB-SECRET" in rules('API_KEY = "Zr8Qw3Nm6Pk1Vx9Lb4Tc7Hj2Fd5Gs0Ya"\n')
    assert {f.rule_id for f in scan_template("{{ x|safe }}", "t.html")} == {"RB-JINJA-SAFE"}


def test_safe_code_is_quiet():
    safe = """
import yaml
def f(db, q, s):
    db.execute("SELECT * FROM t WHERE a = ?", (q,))
    return yaml.safe_load(s)
"""
    assert rules(safe) == set()


def test_demo_gate_fails_with_expected_findings(demo):
    report, decision = run_gate(
        parse_config({"app": "app:app", "scanners": {"pip_audit": False}}), demo, use_ai=False
    )
    found = {f.rule_id for f in report.open_findings()}
    expected = {
        "RB-SQLI",
        "RB-XSS-HTMLRESPONSE",
        "RB-OPEN-REDIRECT",
        "RB-YAML-LOAD",
        "RB-SECRET",
        "RB-JINJA-SAFE",
        "RB-DAST-REFLECTED",
        "RB-DAST-OPEN-REDIRECT",
        "RB-DAST-CSP",
    }
    assert expected <= found, expected - found
    assert not decision.passed
    assert report.preview.mode == "inprocess"


def test_hardened_app_passes(hardened):
    report, decision = run_gate(
        parse_config({"app": "app:app", "scanners": {"pip_audit": False}}), hardened, use_ai=False
    )
    assert decision.passed, [f.title for f in decision.blocking]
    assert not [f for f in report.open_findings() if f.tool == "dast"]


def test_decide_threshold():
    from redblue.gate.schemas import ScanReport

    r = ScanReport(findings=[Finding(tool="x", rule_id="a", title="t", severity=Severity.medium)])
    assert decide(r, Severity.high).passed
    assert not decide(r, Severity.medium).passed
