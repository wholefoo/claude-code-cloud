"""Scaffold step: write a normal, ejectable FastAPI project from an approved plan.

The output depends on the redblue packages like any other library. There is no hidden
builder runtime: delete RedBlue's CLI and the project still runs with uvicorn."""

from __future__ import annotations

import json
import secrets
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from redblue.builder.plan import BuildPlan

TEMPLATE_ROOT = Path(__file__).parent / "project_template"


class ScaffoldError(RuntimeError):
    pass


def scaffold(plan: BuildPlan, dest: Path, *, overwrite: bool = False) -> list[Path]:
    dest = Path(dest)
    if dest.exists() and any(dest.iterdir()) and not overwrite:
        raise ScaffoldError(f"{dest} is not empty.")
    # Renders Python/YAML/Markdown project files, not HTML: escaping would corrupt them.
    # Inputs are the validated BuildPlan. Accepted in .redblue.yml with the same reason.
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATE_ROOT)),
        undefined=StrictUndefined,
        keep_trailing_newline=True,
        autoescape=False,  # noqa: S701  # nosemgrep
    )
    ctx = {
        "plan": plan,
        "secret": secrets.token_urlsafe(48),
        "package": "".join(c if c.isalnum() else "_" for c in dest.name.lower()) or "site",
    }
    written: list[Path] = []
    for src in sorted(TEMPLATE_ROOT.rglob("*")):
        if src.is_dir() or "__pycache__" in src.parts:
            continue
        rel = src.relative_to(TEMPLATE_ROOT).as_posix()
        out_rel = rel.removesuffix(".tmpl")
        target = dest / out_rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if rel.endswith(".tmpl"):
            target.write_text(env.get_template(rel).render(**ctx), encoding="utf-8")
        else:
            target.write_bytes(src.read_bytes())
        written.append(target)
    # A real .env with a fresh secret, never committed (.gitignore lists it).
    env_example = (dest / ".env.example").read_text(encoding="utf-8")
    env_file = dest / ".env"
    env_file.write_text(
        env_example.replace("RB_SECRET_KEY=\n", f"RB_SECRET_KEY={ctx['secret']}\n"),
        encoding="utf-8",
    )
    env_file.chmod(0o600)
    written.append(env_file)
    (dest / "content").mkdir(exist_ok=True)
    plan_path = dest / "content" / "plan.json"
    plan_path.write_text(plan.model_dump_json(indent=2), encoding="utf-8")
    written.append(plan_path)
    return written


def seed_from_plan(db, plan: BuildPlan, author) -> int:
    """Create one draft per planned page. Drafts contain TODO(editor) markers, so the
    publish guardrails keep placeholder pages off the live site until a human writes them."""
    from sqlalchemy import select

    from redblue.cms import service
    from redblue.cms.models import Entry
    from redblue.templates.registry import CATALOG

    n = 0
    for p in plan.pages:
        pt = CATALOG[p.page_type]
        if pt.listing_of or pt.collection not in (None, "page"):
            continue  # listings render automatically from their collection
        slug = "home" if p.page_type == "home" else p.slug
        if db.scalar(select(Entry.id).where(Entry.collection == "page", Entry.slug == slug)):
            continue
        blocks = [
            {
                "type": "answer",
                "question": q,
                "answer": "TODO(editor): write a direct 1–3 sentence answer.",
            }
            for q in (plan.questions[:2] if p.page_type == "home" else [])
        ]
        blocks.append({"type": "paragraph", "text": f"TODO(editor): {p.purpose}."})
        if p.page_type in ("contact", "newsletter_signup", "waitlist"):
            blocks.append(
                {"type": "form", "kind": {"contact": "contact"}.get(p.page_type, "newsletter")}
            )
        service.create_entry(
            db,
            "page",
            service.EntryInput(
                title=p.title if p.page_type != "home" else plan.site_name,
                slug=slug,
                summary=plan.description if p.page_type == "home" else p.purpose,
                data={"page_type": p.page_type},
                blocks=blocks,
                tags=["nav"] if p.page_type in ("about", "pricing", "contact") else [],
                seo={"target_questions": plan.questions if p.page_type == "home" else []},
            ),
            author,
        )
        n += 1
    return n


def plan_from_project(project: Path) -> BuildPlan:
    return BuildPlan.model_validate(json.loads((project / "content" / "plan.json").read_text()))
