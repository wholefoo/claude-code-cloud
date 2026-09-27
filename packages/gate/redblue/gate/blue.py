"""Blue agent: turns findings into fixes, each proven by a regression test that fails before
the fix and passes after. Fixes are opened as pull requests for humans; never merged.

Deterministic fixers handle common, mechanical cases; everything else goes to the model via
structured output (when an API key is available)."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
from collections.abc import Callable
from pathlib import Path

import httpx
from pydantic import BaseModel, Field

from redblue.gate.config import GateConfig
from redblue.gate.llm import LLM, untrusted
from redblue.gate.schemas import FileChange, Finding, FixProposal, RegressionTest

BLUE_SYSTEM = """You are the Blue agent in a defensive security gate for a FastAPI project.
Given one finding and the relevant source, produce the smallest correct fix and a pytest
regression test that FAILS on the current code and PASSES once the fix is applied.
Rules: change as few lines as possible; keep behaviour for legitimate input; do not add
dependencies; the test must not use the network; return complete new file contents for every
file you change; test paths go under tests/security/."""


class LLMFix(BaseModel):
    summary: str = Field(max_length=200)
    rationale: str = Field(max_length=2000)
    files: list[FileChange]
    regression_test: RegressionTest


Fixer = Callable[[Path, Finding], FixProposal | None]
FIXERS: dict[str, Fixer] = {}


def fixer(*rule_ids: str):
    def deco(fn: Fixer) -> Fixer:
        for r in rule_ids:
            FIXERS[r] = fn
        return fn

    return deco


def _test_name(f: Finding) -> str:
    return f"tests/security/test_rb_{f.fingerprint[:10]}.py"


def _static_test(f: Finding, check_body: str) -> RegressionTest:
    """A regression test that parses the source file and asserts the unsafe pattern is gone."""
    body = textwrap.indent(textwrap.dedent(check_body).strip(), "    ")
    content = f'''"""RedBlue regression test for {f.rule_id} ({f.cwe or "n/a"}) at {f.location}."""
import ast
import pathlib

SOURCE = pathlib.Path(__file__).resolve().parents[2] / {f.file!r}


def test_{f.rule_id.lower().replace("-", "_")}_{f.fingerprint[:8]}():
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
{body}
'''
    return RegressionTest(path=_test_name(f), content=content)


def _read(repo: Path, f: Finding) -> str | None:
    if not f.file:
        return None
    try:
        return (repo / f.file).read_text(encoding="utf-8")
    except OSError:
        return None


def _replace_line(source: str, lineno: int, fn: Callable[[str], str]) -> str | None:
    lines = source.splitlines(keepends=True)
    if not 1 <= lineno <= len(lines):
        return None
    new = fn(lines[lineno - 1])
    if new == lines[lineno - 1]:
        return None
    lines[lineno - 1] = new
    return "".join(lines)


@fixer("RB-YAML-LOAD", "B506")
def fix_yaml(repo: Path, f: Finding) -> FixProposal | None:
    src = _read(repo, f)
    if src is None or not f.line:
        return None

    def edit(line: str) -> str:
        line = re.sub(r"\byaml\.(?:unsafe_)?load\(", "yaml.safe_load(", line)
        return re.sub(r",\s*Loader\s*=\s*[\w.]+", "", line)

    new = _replace_line(src, f.line, edit)
    if new is None:
        return None
    test = _static_test(
        f,
        """
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr in ("load", "unsafe_load") and getattr(
                        node.func.value, "id", "") == "yaml":
                    loader = [k for k in node.keywords if k.arg == "Loader"]
                    assert loader and "Safe" in ast.unparse(loader[0].value), (
                        f"unsafe yaml.load at line {node.lineno}")
    """,
    )
    return FixProposal(
        finding_id=f.id,
        fingerprint=f.fingerprint,
        rule_id=f.rule_id,
        summary=f"Use yaml.safe_load in {f.file}",
        rationale="yaml.load can construct arbitrary Python objects from input.",
        files=[FileChange(path=f.file, new_content=new)],
        regression_test=test,
    )


def _kw_flag_fixer(kw: str, bad: str, good: str, rule: str, summary: str, why: str) -> Fixer:
    def fix(repo: Path, f: Finding) -> FixProposal | None:
        src = _read(repo, f)
        if src is None or not f.line:
            return None
        new = _replace_line(
            src, f.line, lambda s: re.sub(rf"\b{kw}\s*=\s*{bad}\b", f"{kw}={good}", s)
        )
        if new is None:
            return None
        test = _static_test(
            f,
            f"""
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    for k in node.keywords:
                        assert not (k.arg == {kw!r} and isinstance(k.value, ast.Constant)
                                    and k.value.value is {bad}), (
                            f"{kw}={bad} at line {{node.lineno}}")
        """,
        )
        return FixProposal(
            finding_id=f.id,
            fingerprint=f.fingerprint,
            rule_id=rule,
            summary=summary.format(file=f.file),
            rationale=why,
            files=[FileChange(path=f.file, new_content=new)],
            regression_test=test,
        )

    return fix


FIXERS["RB-TLS-VERIFY"] = FIXERS["B501"] = _kw_flag_fixer(
    "verify",
    "False",
    "True",
    "RB-TLS-VERIFY",
    "Re-enable TLS verification in {file}",
    "Disabling certificate verification allows man-in-the-middle attacks.",
)
FIXERS["RB-DEBUG"] = _kw_flag_fixer(
    "debug",
    "True",
    "False",
    "RB-DEBUG",
    "Disable debug mode in {file}",
    "Debug mode exposes stack traces and internals to visitors.",
)


def parameterize_sql(sql: str) -> tuple[str, list[str]] | None:
    """Turn the body of an f-string SQL query into (qmark SQL, parameter expressions).

    ``'%{q}%'`` becomes ``?`` with parameter ``f"%{q}%"``; ``'{name}'`` and bare ``{n}``
    become ``?`` with parameters ``name`` / ``n``. Returns None when unsure.
    """
    params: list[str] = []
    out: list[str] = []
    i = 0
    while i < len(sql):
        ch = sql[i]
        if ch in "'\"":
            j = sql.find(ch, i + 1)
            if j == -1:
                return None
            literal = sql[i + 1 : j]
            if "{" in literal:
                whole = re.fullmatch(r"\{([^{}]+)\}", literal)
                params.append(whole.group(1) if whole else "f" + repr(literal))
                out.append("?")
            else:
                out.append(sql[i : j + 1])
            i = j + 1
        elif ch == "{":
            j = sql.find("}", i)
            if j == -1 or sql[i + 1 : j].strip() == "":
                return None
            params.append(sql[i + 1 : j])
            out.append("?")
            i = j + 1
        else:
            out.append(ch)
            i += 1
    new_sql = "".join(out)
    if not params or "{" in new_sql or "}" in new_sql:
        return None
    return new_sql, params


@fixer("RB-SQLI", "B608")
def fix_sqli(repo: Path, f: Finding) -> FixProposal | None:
    """Rewrite a one-line ``execute(f"...")`` into a DB-API parameterised query."""
    src = _read(repo, f)
    if src is None or not f.line:
        return None
    line = src.splitlines()[f.line - 1]
    m = re.search(r"""\.execute\(\s*f(["'])(.*?)\1\s*\)""", line)
    if not m:
        return None
    converted = parameterize_sql(m.group(2))
    if converted is None:
        return None
    new_sql, exprs = converted
    q = m.group(1)
    if q in new_sql:
        return None
    params = ", ".join(exprs) + ("," if len(exprs) == 1 else "")
    new_call = f".execute({q}{new_sql}{q}, ({params}))"
    new = _replace_line(src, f.line, lambda s: s.replace(m.group(0), new_call))
    if new is None:
        return None
    test = _static_test(
        f,
        """
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "execute" and node.args):
                assert not isinstance(node.args[0], ast.JoinedStr), (
                    f"SQL built with an f-string at line {node.lineno}")
                if isinstance(node.args[0], ast.Constant) and "?" in str(node.args[0].value):
                    sql = node.args[0].value
                    assert not any(seg.count("?") for seg in sql.split("'")[1::2]), (
                        "placeholder inside a quoted literal is not bound")
    """,
    )
    return FixProposal(
        finding_id=f.id,
        fingerprint=f.fingerprint,
        rule_id=f.rule_id,
        summary=f"Parameterise SQL query in {f.file}",
        rationale="Interpolating input into SQL allows injection; bound "
        "parameters keep data separate from the query.",
        files=[FileChange(path=f.file, new_content=new)],
        regression_test=test,
    )


@fixer("RB-JINJA-SAFE")
def fix_jinja_safe(repo: Path, f: Finding) -> FixProposal | None:
    src = _read(repo, f)
    if src is None or not f.line:
        return None
    new = _replace_line(src, f.line, lambda s: re.sub(r"\s*\|\s*safe\b", "", s))
    if new is None:
        return None
    content = f'''"""RedBlue regression test for {f.rule_id} at {f.location}."""
import pathlib
import re

TEMPLATE = pathlib.Path(__file__).resolve().parents[2] / {f.file!r}


def test_no_safe_filter_{f.fingerprint[:8]}():
    assert not re.search(r"\\|\\s*safe\\b", TEMPLATE.read_text(encoding="utf-8")), (
        "|safe disables autoescaping; render user content escaped")
'''
    return FixProposal(
        finding_id=f.id,
        fingerprint=f.fingerprint,
        rule_id=f.rule_id,
        summary=f"Remove |safe from {f.file}",
        rationale="|safe renders content as raw HTML, enabling stored XSS.",
        files=[FileChange(path=f.file, new_content=new)],
        regression_test=RegressionTest(path=_test_name(f), content=content),
    )


class BlueAgent:
    def __init__(self, config: GateConfig, repo: Path, llm: LLM | None = None):
        self.config, self.repo, self.llm = config, repo.resolve(), llm

    def propose(self, f: Finding) -> FixProposal | None:
        fx = FIXERS.get(f.rule_id)
        if fx:
            proposal = fx(self.repo, f)
            if proposal:
                return proposal
        if self.llm and self.llm.available:
            return self._llm_fix(f)
        return None

    def _llm_fix(self, f: Finding) -> FixProposal | None:
        src = _read(self.repo, f) or ""
        prompt = (
            f"Finding {f.rule_id} ({f.cwe}) severity {f.severity.value} at {f.location}: "
            f"{f.title}\n\n"
            + untrusted("finding-evidence", f.evidence or f.description)
            + ("\n\n" + untrusted(f"file {f.file}", src[:60_000]) if src else "")
            + "\n\nThe app entry point is "
            + str(self.config.app)
        )
        try:
            fix = self.llm.structured(
                model=self.config.model_blue,
                system=BLUE_SYSTEM,
                prompt=prompt,
                output=LLMFix,
                task=f"blue.fix:{f.id}",
            )
        except Exception:  # noqa: BLE001 - fall back to "no fix"; the finding stays open
            return None
        for change in [*fix.files, fix.regression_test]:
            if not _safe_rel(self.repo, change.path):
                return None
        return FixProposal(
            finding_id=f.id,
            fingerprint=f.fingerprint,
            rule_id=f.rule_id,
            summary=fix.summary,
            rationale=fix.rationale,
            files=fix.files,
            regression_test=fix.regression_test,
            source="llm",
        )

    # ------------------------------------------------------------------ verification

    def verify(self, p: FixProposal, timeout: int = 300) -> FixProposal:
        """Copy the repo, add the test (must fail), apply the fix (test must pass), then run
        the project's existing tests."""
        log: list[str] = []
        with tempfile.TemporaryDirectory(prefix="rb-verify-") as tmp:
            work = Path(tmp) / "repo"
            shutil.copytree(
                self.repo,
                work,
                ignore=shutil.ignore_patterns(
                    ".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache"
                ),
            )
            test_path = work / p.regression_test.path
            test_path.parent.mkdir(parents=True, exist_ok=True)
            test_path.write_text(p.regression_test.content, encoding="utf-8")
            before = _pytest(work, [p.regression_test.path], timeout)
            log.append(f"before fix: exit {before.returncode}")
            if before.returncode == 0:
                p.verification_log = "\n".join(
                    log + ["test passed before the fix; rejected", before.stdout[-1500:]]
                )
                return p
            for change in p.files:
                (work / change.path).write_text(change.new_content, encoding="utf-8")
            after = _pytest(work, [p.regression_test.path], timeout)
            log.append(f"after fix: exit {after.returncode}")
            if after.returncode != 0:
                p.verification_log = "\n".join(log + [after.stdout[-1500:]])
                return p
            suite = _pytest(work, [], timeout) if (work / "tests").exists() else None
            if suite is not None:
                log.append(f"full suite: exit {suite.returncode}")
                if suite.returncode not in (0, 5):
                    p.verification_log = "\n".join(log + [suite.stdout[-1500:]])
                    return p
        p.verified = True
        p.verification_log = "\n".join(log)
        return p

    # ------------------------------------------------------------------ output

    def write(self, p: FixProposal, out_dir: Path) -> Path:
        """Dry run: write the patched files and test under ``out_dir/<fingerprint>``."""
        dest = out_dir / p.fingerprint[:10]
        for change in [*p.files, p.regression_test]:
            path = dest / change.path
            path.parent.mkdir(parents=True, exist_ok=True)
            content = change.new_content if isinstance(change, FileChange) else change.content
            path.write_text(content, encoding="utf-8")
        (dest / "proposal.json").write_text(p.model_dump_json(indent=2), encoding="utf-8")
        return dest

    def open_pr(self, p: FixProposal, base: str) -> str | None:
        """Push a branch and open a PR. Only inside GitHub Actions with a token. Never merges
        and never enables auto-merge."""
        token, repo_slug = os.environ.get("GITHUB_TOKEN"), os.environ.get("GITHUB_REPOSITORY")
        if not (token and repo_slug and os.environ.get("GITHUB_ACTIONS") == "true"):
            return None
        if not p.verified:
            return None
        branch = f"redblue/fix-{p.fingerprint[:8]}"
        git = ["git", "-C", str(self.repo)]
        _run([*git, "checkout", "-B", branch])
        for change in [*p.files, p.regression_test]:
            content = change.new_content if isinstance(change, FileChange) else change.content
            target = self.repo / change.path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        _run([*git, "add", *[c.path for c in p.files], p.regression_test.path])
        _run(
            [
                *git,
                "-c",
                "user.name=redblue-bot",
                "-c",
                "user.email=redblue-bot@users.noreply.github.com",
                "commit",
                "-m",
                f"security: {p.summary}\n\nFixes {p.rule_id} (finding {p.finding_id}).\n"
                "Includes a failing-then-passing regression test.",
            ]
        )
        _run([*git, "push", "--force-with-lease", "origin", f"HEAD:refs/heads/{branch}"])
        _run([*git, "checkout", "-"])
        body = (
            f"**RedBlue Blue agent fix** for `{p.rule_id}`\n\n{p.rationale}\n\n"
            f"- Regression test: `{p.regression_test.path}` (fails before, passes after)\n"
            f"- Source: {p.source}\n\nThis PR was opened automatically and will not be "
            "merged automatically. Please review it like any other change."
        )
        resp = httpx.post(
            f"https://api.github.com/repos/{repo_slug}/pulls",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
            json={
                "title": f"security: {p.summary}",
                "head": branch,
                "base": base,
                "body": body,
                "maintainer_can_modify": True,
            },
            timeout=30,
        )
        if resp.status_code == 422 and "already exists" in resp.text:
            return None
        resp.raise_for_status()
        p.pr_url = resp.json().get("html_url")
        return p.pr_url


def _safe_rel(repo: Path, rel: str) -> bool:
    if not rel or rel.startswith(("/", "\\")) or ".." in Path(rel).parts or ".git" in rel:
        return False
    return (repo / rel).resolve().is_relative_to(repo)


def _pytest(cwd: Path, args: list[str], timeout: int) -> subprocess.CompletedProcess:
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    env.pop("ANTHROPIC_API_KEY", None)  # tests of user code never see the key
    try:
        return subprocess.run(  # noqa: S603 - fixed argv
            [
                sys.executable,
                "-m",
                "pytest",
                "-q",
                "-x",
                "-p",
                "no:cacheprovider",
                "--rootdir",
                str(cwd),
                *args,
            ],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        return subprocess.CompletedProcess(exc.cmd, 124, "timeout", "")


def _run(cmd: list[str]) -> None:
    subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=120)  # noqa: S603


def proposals_to_json(ps: list[FixProposal]) -> str:
    return json.dumps([p.model_dump(mode="json") for p in ps], indent=2)
