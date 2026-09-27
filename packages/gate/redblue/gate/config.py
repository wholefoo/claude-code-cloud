"""`.redblue.yml` loading and validation.

There is deliberately **no target URL field**. The Red agent only scans previews it
launched itself (see :mod:`redblue.gate.preview`). Configs that try to point it at a URL
are rejected outright rather than silently ignored.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from redblue.gate.schemas import Severity

CONFIG_FILENAME = ".redblue.yml"

FORBIDDEN_KEYS = frozenset({"target", "targets", "url", "target_url", "base_url"})
TARGET_URL_ERROR = (
    "`.redblue.yml` must not contain `{key}`. The RedBlue Red agent never scans an "
    "arbitrary URL: it only probes a preview of your app that it launched itself on "
    "127.0.0.1 (configure `app: module:attr` or `start_command` instead)."
)

DEFAULT_EXCLUDE = [
    ".git",
    ".venv",
    "venv",
    "env",
    "node_modules",
    "__pycache__",
    "build",
    "dist",
    "*.egg-info",
    ".redblue",
    ".tox",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
]

DEFAULT_MODEL_RED = "claude-haiku-4-5-20251001"
DEFAULT_MODEL_BLUE = "claude-sonnet-5"


class ConfigError(ValueError):
    """The config file is invalid."""


class ScannerToggles(BaseModel):
    model_config = ConfigDict(extra="forbid")

    builtin_sast: bool = True
    bandit: bool = True
    semgrep: bool = True
    pip_audit: bool = True
    dast: bool = True
    zap: bool = True
    nuclei: bool = True

    def enabled(self, name: str) -> bool:
        return bool(getattr(self, name, False))


class IgnoreRule(BaseModel):
    """Accept a finding. Every ignore needs a human-written reason."""

    model_config = ConfigDict(extra="forbid")

    fingerprint: str | None = None
    rule_id: str | None = None
    path: str | None = Field(default=None, description="Optional glob limiting a rule_id ignore.")
    reason: str = Field(min_length=3)

    @model_validator(mode="after")
    def _one_selector(self) -> IgnoreRule:
        if not (self.fingerprint or self.rule_id):
            raise ValueError("an ignore entry needs `fingerprint` or `rule_id`")
        if self.fingerprint and len(self.fingerprint) < 8:
            raise ValueError("fingerprint prefixes must be at least 8 characters")
        return self


class ModelConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    red: str | None = None
    blue: str | None = None


class GateConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stack: Literal["fastapi"] = "fastapi"
    app: str | None = Field(default=None, description="ASGI import path, e.g. `app.main:app`.")
    app_dir: str = "."
    start_command: str | None = Field(
        default=None, description="Optional server command; `{port}` is substituted."
    )
    seed_command: str | None = None
    preview_mode: Literal["auto", "inprocess", "subprocess"] = "auto"
    severity_threshold: Severity = Severity.high
    scanners: ScannerToggles = Field(default_factory=ScannerToggles)
    ignore: list[IgnoreRule] = Field(default_factory=list)
    paths: list[str] = Field(default_factory=lambda: ["."])
    exclude: list[str] = Field(default_factory=list)
    spend_limit_usd: float = Field(default=1.0, ge=0)
    probe_paths: list[str] = Field(default_factory=list)
    max_requests: int = Field(default=400, ge=10, le=5000)
    models: ModelConfig = Field(default_factory=ModelConfig)

    @field_validator("severity_threshold", mode="before")
    @classmethod
    def _sev(cls, v: Any) -> Severity:
        return Severity.parse(v)

    @field_validator("app")
    @classmethod
    def _app_path(cls, v: str | None) -> str | None:
        if v is None:
            return v
        mod, sep, attr = v.partition(":")
        if not sep or not mod or not attr or "/" in v or "//" in v:
            raise ValueError("`app` must be an import path like `package.module:app`")
        return v

    @field_validator("start_command")
    @classmethod
    def _start(cls, v: str | None) -> str | None:
        if v is not None and "{port}" not in v:
            raise ValueError("`start_command` must contain `{port}` so RedBlue picks the port")
        return v

    @field_validator("probe_paths")
    @classmethod
    def _probe_paths(cls, v: list[str]) -> list[str]:
        for p in v:
            parts = urlsplit(p)
            if parts.scheme or parts.netloc or not p.startswith("/") or p.startswith("//"):
                raise ValueError(
                    f"probe_paths entries must be app-relative paths starting with '/', got "
                    f"{p!r}. RedBlue never probes external hosts."
                )
        return v

    @property
    def all_excludes(self) -> list[str]:
        return DEFAULT_EXCLUDE + list(self.exclude)

    @property
    def model_red(self) -> str:
        return os.environ.get("RB_MODEL_RED") or self.models.red or DEFAULT_MODEL_RED

    @property
    def model_blue(self) -> str:
        return os.environ.get("RB_MODEL_BLUE") or self.models.blue or DEFAULT_MODEL_BLUE


def _reject_target_keys(data: Any, where: str = "") -> None:
    if isinstance(data, dict):
        for key, value in data.items():
            if isinstance(key, str) and key.strip().lower() in FORBIDDEN_KEYS:
                raise ConfigError(TARGET_URL_ERROR.format(key=f"{where}{key}"))
            _reject_target_keys(value, f"{where}{key}.")
    elif isinstance(data, list):
        for item in data:
            _reject_target_keys(item, where)


def parse_config(data: Any) -> GateConfig:
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ConfigError("`.redblue.yml` must be a mapping at the top level")
    _reject_target_keys(data)
    try:
        return GateConfig.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(f"Invalid .redblue.yml:\n{exc}") from exc


def load_config(path: str | Path | None = None, repo_path: str | Path = ".") -> GateConfig:
    """Load ``path`` (default ``<repo>/.redblue.yml``). A missing default file yields defaults."""
    repo = Path(repo_path)
    explicit = path is not None
    cfg_path = Path(path) if path is not None else repo / CONFIG_FILENAME
    if not cfg_path.is_absolute() and not cfg_path.exists() and (repo / cfg_path).exists():
        cfg_path = repo / cfg_path
    if not cfg_path.exists():
        if explicit and Path(path).name != CONFIG_FILENAME:  # type: ignore[arg-type]
            raise ConfigError(f"Config file not found: {cfg_path}")
        return GateConfig()
    try:
        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"{cfg_path}: invalid YAML: {exc}") from exc
    return parse_config(data)
