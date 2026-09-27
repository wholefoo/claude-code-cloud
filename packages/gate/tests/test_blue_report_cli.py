import json

from redblue.gate import cli, report
from redblue.gate.blue import BlueAgent, parameterize_sql
from redblue.gate.config import parse_config
from redblue.gate.gate import run_gate


def test_parameterize_sql():
    assert parameterize_sql("SELECT * FROM t WHERE n LIKE '%{q}%'") == (
        "SELECT * FROM t WHERE n LIKE ?",
        ["f'%{q}%'"],
    )
    assert parameterize_sql("SELECT 1") is None


def test_blue_fixes_verified(demo):
    cfg = parse_config({"app": "app:app", "scanners": {"pip_audit": False}})
    rep, _ = run_gate(cfg, demo, use_ai=False, fix=True)
    by_rule = {p.rule_id: p for p in rep.fixes}
    assert by_rule["RB-SQLI"].verified, by_rule["RB-SQLI"].verification_log
    assert by_rule["RB-YAML-LOAD"].verified, by_rule["RB-YAML-LOAD"].verification_log
    # The repo itself is untouched (fixes are proposals, never applied in place).
    assert "yaml.load(" in (demo / "app.py").read_text()


def test_blue_rejects_test_that_passes_before_fix(demo):
    from redblue.gate.schemas import FixProposal, RegressionTest

    blue = BlueAgent(parse_config({}), demo)
    p = FixProposal(
        finding_id="x",
        summary="noop",
        regression_test=RegressionTest(
            path="tests/security/test_noop.py", content="def test_ok():\n    assert True\n"
        ),
    )
    assert not blue.verify(p).verified


def test_blue_never_opens_prs_outside_actions(demo, monkeypatch):
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    from redblue.gate.schemas import FixProposal, RegressionTest

    p = FixProposal(
        finding_id="x",
        summary="s",
        verified=True,
        regression_test=RegressionTest(path="t.py", content=""),
    )
    assert BlueAgent(parse_config({}), demo).open_pr(p, "main") is None


def test_sarif_and_markdown(demo):
    rep, dec = run_gate(
        parse_config({"app": "app:app", "scanners": {"pip_audit": False}}), demo, use_ai=False
    )
    s = report.sarif(rep)
    assert s["version"] == "2.1.0" and s["runs"][0]["results"]
    assert all("partialFingerprints" in r for r in s["runs"][0]["results"])
    # Accepted findings stay out of SARIF (code scanning ignores suppressions) but remain
    # available on request and in the Markdown report.
    from redblue.gate.schemas import FindingStatus

    rep.findings[0].status = FindingStatus.accepted
    accepted_fp = rep.findings[0].fingerprint
    fps = {r["partialFingerprints"]["redblue/v1"] for r in report.sarif(rep)["runs"][0]["results"]}
    assert accepted_fp not in fps
    full = report.sarif(rep, include_suppressed=True)["runs"][0]["results"]
    assert any(r.get("suppressions") for r in full)
    md = report.markdown(rep, dec)
    assert "Gate failed" in md and "| Severity |" in md


def test_cli_exit_codes(demo, hardened, capsys):
    assert cli.main(["scan", "--path", str(demo), "--no-ai", "--format", "json"]) == 1
    data = json.loads(capsys.readouterr().out)
    assert data["decision"]["passed"] is False
    assert cli.main(["scan", "--path", str(hardened), "--no-ai"]) == 0


def test_fork_detection(tmp_path, monkeypatch):
    ev = tmp_path / "event.json"
    ev.write_text(
        json.dumps(
            {
                "pull_request": {
                    "head": {"repo": {"fork": True, "full_name": "a/b"}},
                    "base": {"repo": {"full_name": "c/b"}},
                }
            }
        )
    )
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(ev))
    assert cli.is_fork_event()
    ev.write_text(
        json.dumps(
            {
                "pull_request": {
                    "head": {"repo": {"fork": False, "full_name": "c/b"}},
                    "base": {"repo": {"full_name": "c/b"}},
                }
            }
        )
    )
    assert not cli.is_fork_event()
