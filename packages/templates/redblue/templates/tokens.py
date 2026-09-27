"""Design tokens: a site's look changes in one place. Rendered to CSS custom properties."""

from __future__ import annotations

import re

from pydantic import BaseModel, field_validator

_COLOR = re.compile(r"^#[0-9a-fA-F]{3,8}$")


class Palette(BaseModel):
    bg: str
    surface: str
    text: str
    muted: str
    border: str
    primary: str
    primary_text: str
    accent: str
    danger: str
    success: str
    warning: str

    @field_validator("*")
    @classmethod
    def _hex(cls, v: str) -> str:
        if not _COLOR.match(v):
            raise ValueError("Colors must be hex values like #1a2b3c.")
        return v


class Tokens(BaseModel):
    font_sans: str = (
        "system-ui, -apple-system, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif"
    )
    font_mono: str = "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace"
    radius: str = "10px"
    max_width: str = "72rem"
    prose_width: str = "44rem"
    light: Palette = Palette(
        bg="#ffffff",
        surface="#f6f7f9",
        text="#14161a",
        muted="#555b66",
        border="#dde1e7",
        primary="#1f4fd1",
        primary_text="#ffffff",
        accent="#c2185b",
        danger="#b3261e",
        success="#1b7a3d",
        warning="#8a5a00",
    )
    dark: Palette = Palette(
        bg="#0f1115",
        surface="#171a21",
        text="#e8eaef",
        muted="#a3a9b5",
        border="#2a2f3a",
        primary="#7aa2ff",
        primary_text="#0b0d12",
        accent="#ff7eb6",
        danger="#ff8a80",
        success="#7ddc9b",
        warning="#ffcc66",
    )

    @field_validator("font_sans", "font_mono", "radius", "max_width", "prose_width")
    @classmethod
    def _no_css_injection(cls, v: str) -> str:
        if re.search(r"[;{}<>\\]|url\(|expression", v, re.I):
            raise ValueError("Invalid token value.")
        return v

    def css(self) -> str:
        def vars_(p: Palette) -> str:
            return "".join(f"--rb-{k.replace('_', '-')}:{v};" for k, v in p.model_dump().items())

        common = (
            f"--rb-font-sans:{self.font_sans};--rb-font-mono:{self.font_mono};"
            f"--rb-radius:{self.radius};--rb-max:{self.max_width};"
            f"--rb-prose:{self.prose_width};"
        )
        return (
            f":root{{{common}{vars_(self.light)}color-scheme:light dark}}"
            f"@media (prefers-color-scheme:dark){{:root:not([data-theme=light]){{"
            f"{vars_(self.dark)}}}}}"
            f":root[data-theme=dark]{{{vars_(self.dark)}}}"
        )
