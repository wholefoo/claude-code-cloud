"""Builder agent: plan → implement → verify, always on a branch, never on main.

A change is only "done" when the project's tests pass and the security gate is green. The
result is a branch (and a PR when running in GitHub Actions) for a human to review."""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel, Field

from redblue.core.ai import AIUnavailable, untrusted

IMPLEMENT_SYSTEM = """You are the RedBlue builder agent working in a FastAPI + Jinja2 + HTMX
project built on the redblue packages. Implement the requested change with the smallest set of
file edits, and add or update pytest tests that prove it works. Keep Jinja autoescape on and
never use |safe; validate input with Pydantic; use parameterised queries only; no secrets in
code. Content belongs in the CMS, not templates. Return complete new contents for each file."""

MAX_CONTEXT_FILES = 40


class FileEdit(BaseModel):
    path: str
    content: str


class Implementation(BaseModel):
    summary: str = Field(max_length=200)
    plan: list[str] = Field(max_length=15)
    files: list[FileEdit]
    tests: list[FileEdit] = Field(min_length=1)


@dataclass
class BuildResult:
    branch: str
    summary: str
    tests_passed: bool
    gate_passed: bool | None
    log: list[str] = field(default_factory=list)

    @property
    def done(self) -> bool:
        return self.tests_passed and self.gate_passed is not False


def _git(project: Path, *args: str) -> str:
    return subprocess.run(  # noqa: S603
        ["git", "-C", str(project), *args],  # noqa: S607
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    ).stdout.strip()


def _safe_path(project: Path, rel: str) -> Path:
    p = (project / rel).resolve()
    if (
        rel.startswith(("/", "\\"))
        or ".." in Path(rel).parts
        or ".git" in Path(rel).parts
        or not p.is_relative_to(project.resolve())
        or Path(rel).name == ".env"
    ):
        raise ValueError(f"Refusing to write outside the project: {rel}")
    return p


def project_context(project: Path) -> str:
    files = [
        p
        for p in sorted(project.rglob("*"))
        if p.is_file()
        and p.suffix in (".py", ".html", ".yml", ".md", ".css")
        and not any(
            part.startswith(".") or part in ("media", "__pycache__", ".venv")
            for part in p.relative_to(project).parts[:-1]
        )
    ][:MAX_CONTEXT_FILES]
    return "\n\n".join(
        untrusted(f"file {p.relative_to(project)}", p.read_text(errors="replace")[:20000])
        for p in files
    )


class BuilderAgent:
    def __init__(self, ai, model: str = "claude-sonnet-5"):
        self.ai, self.model = ai, model

    def implement(
        self, project: Path, request: str, *, branch: str | None = None, run_gate: bool = True
    ) -> BuildResult:
        project = project.resolve()
        if not getattr(self.ai, "available", False):
            raise AIUnavailable("The builder needs ANTHROPIC_API_KEY (bring your own key).")
        current = _git(project, "rev-parse", "--abbrev-ref", "HEAD")
        slug = "".join(c if c.isalnum() else "-" for c in request.lower())[:40].strip("-")
        branch = branch or f"redblue/{slug or 'change'}"
        if branch in ("main", "master") or current == branch:
            raise ValueError("The builder works on its own branch, never on main.")
        impl = self.ai.structured(
            agent="builder",
            model=self.model,
            system=IMPLEMENT_SYSTEM,
            output=Implementation,
            task=f"builder.implement:{slug}",
            prompt=f"Change request:\n{untrusted('request', request)}\n\nProject files:\n"
            + project_context(project),
        )
        _git(project, "checkout", "-b", branch)
        log = [f"branch {branch} from {current}", *impl.plan]
        for edit in [*impl.files, *impl.tests]:
            target = _safe_path(project, edit.path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(edit.content, encoding="utf-8")
        tests = subprocess.run(
            [sys.executable, "-m", "pytest", "-q"],
            cwd=project,  # noqa: S603
            capture_output=True,
            text=True,
            timeout=900,
        )
        log.append(f"pytest exit {tests.returncode}")
        gate_ok = None
        if run_gate:
            try:
                from redblue.gate.config import load_config
                from redblue.gate.gate import run_gate as gate

                _, decision = gate(load_config(None, project), project, use_ai=False)
                gate_ok = decision.passed
                log.append(decision.summary)
            except ImportError:
                log.append("gate not installed; skipped")
        _git(project, "add", "-A")
        _git(
            project,
            "-c",
            "user.name=redblue-builder",
            "-c",
            "user.email=redblue-builder@users.noreply.github.com",
            "commit",
            "-m",
            f"{impl.summary}\n\nGenerated by the RedBlue builder agent. Review before merging.",
        )
        _git(project, "checkout", current)
        return BuildResult(branch, impl.summary, tests.returncode == 0, gate_ok, log)

    def implement_task(self, project: Path, task) -> BuildResult:
        """Accepts an ops-agent BuilderTask (title/description/acceptance criteria)."""
        request = f"{task.title}\n\n{task.description}\n\nAcceptance criteria:\n" + "\n".join(
            f"- {c}" for c in getattr(task, "acceptance_criteria", [])
        )
        return self.implement(project, request)
