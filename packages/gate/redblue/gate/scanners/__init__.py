"""Scanner protocol, shared helpers and the registry."""

from __future__ import annotations

import fnmatch
import shutil
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol, runtime_checkable

from redblue.gate.config import GateConfig
from redblue.gate.schemas import Finding

if TYPE_CHECKING:
    from redblue.gate.preview import Preview


class ScannerUnavailable(RuntimeError):
    """The scanner cannot run here (binary missing, no preview, no input...)."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass
class ScanContext:
    repo_path: Path
    config: GateConfig
    preview: Preview | None = None
    changed_files: list[str] | None = None
    # Probe families for DAST chosen by the Red agent's plan (None = all).
    dast_checks: set[str] | None = None
    notes: list[str] = field(default_factory=list)

    def rel(self, path: Path | str) -> str:
        p = Path(path)
        try:
            p = p.resolve().relative_to(self.repo_path.resolve())
        except ValueError:
            pass
        return p.as_posix()


@runtime_checkable
class Scanner(Protocol):
    name: str
    kind: Literal["sast", "deps", "dast"]

    def run(self, ctx: ScanContext) -> list[Finding]: ...


def is_excluded(rel_path: str, excludes: list[str]) -> bool:
    parts = Path(rel_path).parts
    for pattern in excludes:
        pattern = pattern.rstrip("/")
        if fnmatch.fnmatch(rel_path, pattern) or fnmatch.fnmatch(rel_path, pattern + "/*"):
            return True
        if any(fnmatch.fnmatch(part, pattern) for part in parts):
            return True
    return False


def iter_files(ctx: ScanContext, suffixes: tuple[str, ...]) -> Iterator[Path]:
    root = ctx.repo_path.resolve()
    seen: set[Path] = set()
    for base in ctx.config.paths:
        start = (root / base).resolve()
        if start.is_file():
            candidates = [start]
        elif start.is_dir():
            candidates = sorted(start.rglob("*"))
        else:
            continue
        for path in candidates:
            if not path.is_file() or path.suffix not in suffixes or path in seen:
                continue
            try:
                rel = path.relative_to(root).as_posix()
            except ValueError:
                continue
            if is_excluded(rel, ctx.config.all_excludes):
                continue
            seen.add(path)
            yield path


def find_tool(binary: str, module: str | None = None) -> list[str] | None:
    """Locate a CLI on PATH, falling back to ``python -m module`` when importable."""
    exe = shutil.which(binary)
    if exe:
        return [exe]
    if module:
        import importlib.util

        if importlib.util.find_spec(module) is not None:
            return [sys.executable, "-m", module]
    return None


def run_tool(cmd: list[str], cwd: Path, timeout: int = 600) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(  # noqa: S603 - fixed argv, no shell
            cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired as exc:
        raise ScannerUnavailable(f"{Path(cmd[0]).name} timed out after {timeout}s") from exc
    except OSError as exc:
        raise ScannerUnavailable(f"could not run {cmd[0]}: {exc}") from exc


def is_test_path(path: str | None) -> bool:
    if not path:
        return False
    p = Path(path)
    name = p.name
    return (
        "tests" in p.parts
        or "test" in p.parts
        or name.startswith("test_")
        or name.endswith("_test.py")
        or name == "conftest.py"
    )


def all_scanners() -> dict[str, Scanner]:
    from redblue.gate.scanners.bandit import BanditScanner
    from redblue.gate.scanners.builtin_sast import BuiltinSast
    from redblue.gate.scanners.dast import DastScanner
    from redblue.gate.scanners.nuclei import NucleiScanner
    from redblue.gate.scanners.pip_audit import PipAuditScanner
    from redblue.gate.scanners.semgrep import SemgrepScanner
    from redblue.gate.scanners.zap import ZapScanner

    scanners: list[Scanner] = [
        BuiltinSast(),
        BanditScanner(),
        SemgrepScanner(),
        PipAuditScanner(),
        DastScanner(),
        ZapScanner(),
        NucleiScanner(),
    ]
    return {s.name: s for s in scanners}


__all__ = [
    "ScanContext",
    "Scanner",
    "ScannerUnavailable",
    "all_scanners",
    "find_tool",
    "is_excluded",
    "is_test_path",
    "iter_files",
    "run_tool",
]
