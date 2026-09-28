import json
import subprocess

import pytest

from redblue.gate.config import parse_config
from redblue.gate.red import plan
from redblue.gate.scanners import ScanContext, ScannerUnavailable
from redblue.gate.scanners import pip_audit as pa


def _pyproject(path, name, deps):
    path.mkdir(parents=True, exist_ok=True)
    lines = ",\n  ".join(json.dumps(d) for d in deps)
    (path / "pyproject.toml").write_text(
        f'[project]\nname = "{name}"\nversion = "0.1"\ndependencies = [\n  {lines}\n]\n'
    )


@pytest.fixture
def monorepo(tmp_path):
    _pyproject(tmp_path / "packages/core", "acme-core", ["fastapi>=0.115", "pyyaml>=6"])
    _pyproject(
        tmp_path / "packages/web",
        "acme_web",
        [
            "acme-core",  # the repo's own package: not audited
            "Jinja2[i18n]>=3.1; python_version >= '3.11'",
            "fastapi>=0.115",  # repeated: listed once, against the first file
            "--index-url https://evil.example/simple",  # pip option: dropped
            "-e git+https://evil.example/x.git#egg=x",  # editable URL: dropped
            "evil @ https://evil.example/evil.tar.gz",  # direct reference: dropped
        ],
    )
    _pyproject(tmp_path / "packages/core/tests/fixture", "fixture", ["requests"])  # excluded
    return tmp_path


def _ctx(root):
    cfg = parse_config({"app": "app:app", "paths": ["packages"], "exclude": ["**/tests/*"]})
    return ScanContext(repo_path=root, config=cfg)


def test_collects_third_party_dependencies_safely(monorepo):
    assert pa.project_requirements(_ctx(monorepo)) == {
        "fastapi>=0.115": "packages/core/pyproject.toml",
        "pyyaml>=6": "packages/core/pyproject.toml",
        "Jinja2[i18n]>=3.1; python_version >= '3.11'": "packages/web/pyproject.toml",
    }


def test_audits_declared_dependencies_and_points_at_their_pyproject(monorepo, monkeypatch):
    seen = {}

    def fake_run(cmd, cwd, timeout):
        seen["requirements"] = open(cmd[cmd.index("-r") + 1]).read().splitlines()
        report = {
            "dependencies": [
                {
                    "name": "Jinja2",
                    "version": "3.1.0",
                    "vulns": [{"id": "PYSEC-1", "fix_versions": ["3.1.6"], "aliases": ["CVE-1"]}],
                },
                {
                    "name": "markupsafe",
                    "version": "2.0",
                    "vulns": [{"id": "PYSEC-2", "fix_versions": [], "aliases": []}],
                },
                {"name": "fastapi", "version": "0.141.1", "vulns": []},
            ]
        }
        return subprocess.CompletedProcess(cmd, 1, json.dumps(report), "")

    monkeypatch.setattr(pa, "find_tool", lambda *a: ["pip-audit"])
    monkeypatch.setattr(pa, "run_tool", fake_run)
    findings = pa.PipAuditScanner().run(_ctx(monorepo))

    assert seen["requirements"] == sorted(pa.project_requirements(_ctx(monorepo)))
    jinja, markupsafe = findings
    assert jinja.severity.value == "high" and jinja.file == "packages/web/pyproject.toml"
    assert "fix versions: 3.1.6" in jinja.evidence
    # Transitive (not declared anywhere) and without a fix: medium, first pyproject.
    assert markupsafe.severity.value == "medium"
    assert markupsafe.file == "packages/core/pyproject.toml"


def test_nothing_to_audit_is_reported_as_unavailable(tmp_path, monkeypatch):
    monkeypatch.setattr(pa, "find_tool", lambda *a: ["pip-audit"])
    (tmp_path / "packages").mkdir()
    with pytest.raises(ScannerUnavailable, match="no requirements.txt or pyproject.toml"):
        pa.PipAuditScanner().run(_ctx(tmp_path))


def test_a_change_never_runs_zero_scanners():
    deps_only = ["packages/core/pyproject.toml", ".github/workflows/ci.yml"]
    on = parse_config({"app": "app:app"})
    assert plan(on, deps_only).scanners == ["pip_audit"]
    off = parse_config({"app": "app:app", "scanners": {"pip_audit": False}})
    result = plan(off, deps_only)
    assert result.scanners == ["builtin_sast"]
    assert "chosen scanners are disabled; running baseline SAST" in result.reasons
