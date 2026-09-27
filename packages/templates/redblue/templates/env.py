"""The shared Jinja environment: autoescape always on, CMS block macros, safe filters."""

from __future__ import annotations

from pathlib import Path

from jinja2 import ChoiceLoader, Environment, FileSystemLoader, PackageLoader, select_autoescape
from starlette.templating import Jinja2Templates

from redblue.cms.render import anchor_for, first_answer, inline, toc
from redblue.templates.jsonld import dumps_for_script

TEMPLATE_DIR = Path(__file__).parent / "jinja"
STATIC_DIR = Path(__file__).parent / "static"


def make_environment(extra_dirs: list[Path] | None = None) -> Environment:
    """``extra_dirs`` come first so a site can override any template (ejectable)."""
    loaders = [FileSystemLoader(str(d)) for d in (extra_dirs or [])]
    loaders += [FileSystemLoader(str(TEMPLATE_DIR)), PackageLoader("redblue.cms", "templates")]
    env = Environment(
        loader=ChoiceLoader(loaders),
        autoescape=select_autoescape(default=True, default_for_string=True),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["inline"] = inline
    env.filters["jsonld"] = dumps_for_script
    env.filters["datefmt"] = lambda d, fmt="%B %-d, %Y": d.strftime(fmt) if d else ""
    env.globals.update(anchor_for=anchor_for, toc=toc, first_answer=first_answer)
    return env


def make_templates(extra_dirs: list[Path] | None = None) -> Jinja2Templates:
    return Jinja2Templates(env=make_environment(extra_dirs))
