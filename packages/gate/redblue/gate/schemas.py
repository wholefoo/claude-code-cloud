"""Pydantic v2 schemas shared by scanners, agents, the gate and reports."""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field, model_validator

_ORDER = ("info", "low", "medium", "high", "critical")


class Severity(str, Enum):  # noqa: UP042 - custom ordering
    """Orderable severity: info < low < medium < high < critical."""

    info = "info"
    low = "low"
    medium = "medium"
    high = "high"
    critical = "critical"

    @property
    def rank(self) -> int:
        return _ORDER.index(self.value)

    @classmethod
    def parse(cls, value: str | Severity) -> Severity:
        if isinstance(value, Severity):
            return value
        try:
            return cls(str(value).strip().lower())
        except ValueError as exc:
            raise ValueError(
                f"Unknown severity {value!r}; use one of: {', '.join(_ORDER)}"
            ) from exc

    def _cmp(self, other: object) -> int:
        if not isinstance(other, Severity):
            other = Severity.parse(other)  # type: ignore[arg-type]
        return self.rank - other.rank

    def __lt__(self, other: object) -> bool:  # type: ignore[override]
        return self._cmp(other) < 0

    def __le__(self, other: object) -> bool:  # type: ignore[override]
        return self._cmp(other) <= 0

    def __gt__(self, other: object) -> bool:  # type: ignore[override]
        return self._cmp(other) > 0

    def __ge__(self, other: object) -> bool:  # type: ignore[override]
        return self._cmp(other) >= 0

    def lowered(self, steps: int = 1) -> Severity:
        return Severity(_ORDER[max(0, self.rank - steps)])


class FindingStatus(str, Enum):  # noqa: UP042
    open = "open"
    false_positive = "false_positive"
    fixed = "fixed"
    accepted = "accepted"


Confidence = Literal["low", "medium", "high"]


def _normalize_code(code: str) -> str:
    return re.sub(r"\s+", " ", code or "").strip()


def compute_fingerprint(
    tool: str,
    rule_id: str,
    file: str | None,
    code: str = "",
    endpoint: str | None = None,
    method: str | None = None,
) -> str:
    """Stable identity for a finding. Line numbers are deliberately excluded so the
    fingerprint survives unrelated edits above the finding."""
    parts = [
        tool.lower(),
        rule_id,
        (file or "").replace("\\", "/"),
        _normalize_code(code),
        (method or "").upper(),
        endpoint or "",
    ]
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()


class Finding(BaseModel):
    id: str = ""
    tool: str
    rule_id: str
    title: str
    description: str = ""
    severity: Severity
    confidence: Confidence = "medium"
    cwe: str | None = None
    file: str | None = None
    line: int | None = None
    endpoint: str | None = None
    method: str | None = None
    evidence: str = ""
    code: str = Field(default="", description="Offending source line / probe key.")
    fingerprint: str = ""
    status: FindingStatus = FindingStatus.open
    triage_note: str = ""
    dynamic: bool = Field(default=False, description="Confirmed by a live probe (DAST).")

    @model_validator(mode="after")
    def _fill_ids(self) -> Finding:
        if self.cwe and not self.cwe.upper().startswith("CWE-"):
            self.cwe = f"CWE-{self.cwe}"
        if not self.fingerprint:
            self.fingerprint = compute_fingerprint(
                self.tool, self.rule_id, self.file, self.code, self.endpoint, self.method
            )
        if not self.id:
            self.id = self.fingerprint[:12]
        return self

    @property
    def location(self) -> str:
        if self.file:
            return f"{self.file}:{self.line}" if self.line else self.file
        if self.endpoint:
            return f"{self.method or 'GET'} {self.endpoint}"
        return "-"

    @property
    def is_open(self) -> bool:
        return self.status == FindingStatus.open


class SkippedScanner(BaseModel):
    name: str
    reason: str


class PreviewInfo(BaseModel):
    mode: Literal["inprocess", "subprocess"] | None = None
    base_url: str | None = None
    endpoints: int = 0
    error: str | None = None


def utcnow() -> datetime:
    return datetime.now(UTC)


class FileChange(BaseModel):
    path: str
    new_content: str


class RegressionTest(BaseModel):
    path: str
    content: str


class FixProposal(BaseModel):
    finding_id: str
    fingerprint: str = ""
    rule_id: str = ""
    summary: str
    rationale: str = ""
    files: list[FileChange] = Field(default_factory=list)
    diff: str = ""
    regression_test: RegressionTest
    source: Literal["deterministic", "llm"] = "deterministic"
    related_findings: list[str] = Field(default_factory=list)
    verified: bool = False
    verification_log: str = ""
    pr_url: str | None = None


class ScanReport(BaseModel):
    repo_path: str = "."
    base_ref: str | None = None
    changed_files: list[str] | None = None
    findings: list[Finding] = Field(default_factory=list)
    scanners_run: list[str] = Field(default_factory=list)
    scanners_skipped: list[SkippedScanner] = Field(default_factory=list)
    started_at: datetime = Field(default_factory=utcnow)
    finished_at: datetime | None = None
    preview: PreviewInfo | None = None
    app_file: str | None = None
    ai_used: bool = False
    ai_cost_usd: float = 0.0
    notes: list[str] = Field(default_factory=list)
    fixes: list[FixProposal] = Field(default_factory=list)

    def open_findings(self) -> list[Finding]:
        return [f for f in self.findings if f.is_open]


class GateDecision(BaseModel):
    passed: bool
    threshold: Severity
    blocking: list[Finding] = Field(default_factory=list)
    summary: str = ""
