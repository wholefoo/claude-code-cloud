"""Safe rendering helpers for blocks: inline markup, anchors, table of contents."""

from __future__ import annotations

import html
import re

from markupsafe import Markup, escape
from redblue.cms.service import slugify
from redblue.core.security import is_safe_link

_LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
_BOLD = re.compile(r"\*\*(.+?)\*\*")
_ITALIC = re.compile(r"(?<![*\w])\*(?!\s)(.+?)(?<!\s)\*(?![*\w])")
_CODE = re.compile(r"`([^`]+)`")


def inline(text: str | None, site_host: str = "") -> Markup:
    """Render the tiny inline markup language. Input is escaped first, so the only HTML in
    the output is what this function emits."""
    if not text:
        return Markup("")
    out = str(escape(text))

    def link(m: re.Match) -> str:
        label, url = m.group(1), m.group(2)
        raw = html.unescape(url)
        if not is_safe_link(raw):
            return label
        external = raw.startswith(("http://", "https://")) and (
            not site_host or site_host not in raw.split("/")[2:3]
        )
        rel = ' rel="noopener"' if external else ""
        return f'<a href="{url}"{rel}>{label}</a>'

    codes: list[str] = []

    def stash(m: re.Match) -> str:
        codes.append(f"<code>{m.group(1)}</code>")
        return f"\x00{len(codes) - 1}\x00"

    out = _CODE.sub(stash, out)
    out = _LINK.sub(link, out)
    out = _BOLD.sub(r"<strong>\1</strong>", out)
    out = _ITALIC.sub(r"<em>\1</em>", out)
    out = re.sub(r"\x00(\d+)\x00", lambda m: codes[int(m.group(1))], out)
    return Markup(out)  # noqa: S704 - built only from escaped input and fixed tags


def anchor_for(block: dict) -> str:
    return block.get("anchor") or slugify(block.get("text", ""))[:80]


def toc(blocks: list[dict]) -> list[dict]:
    return [
        {"level": b.get("level", 2), "text": b["text"], "anchor": anchor_for(b)}
        for b in blocks
        if b.get("type") == "heading" and b.get("level", 2) in (2, 3)
    ]


def first_answer(blocks: list[dict]) -> dict | None:
    return next((b for b in blocks if b.get("type") == "answer"), None)
