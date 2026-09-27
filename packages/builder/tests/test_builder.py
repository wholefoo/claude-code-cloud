import json
import subprocess
import sys

import pytest

from redblue.builder.plan import BuildPlan, heuristic_plan
from redblue.builder.scaffold import ScaffoldError, plan_from_project, scaffold, seed_from_plan


def test_heuristic_plan_maps_intent_to_page_types():
    plan = heuristic_plan("A SaaS tool for plumbers with a blog and docs", "PipeDream")
    types = {p.page_type for p in plan.pages}
    assert {
        "home",
        "pricing",
        "features",
        "blog_index",
        "knowledge_base",
        "about",
        "contact",
        "privacy",
    } <= types
    assert "post" in plan.collections and "newsletter" in plan.features
    with pytest.raises(ValueError):
        BuildPlan(
            site_name="x",
            description="y",
            pages=[{"page_type": "nope", "title": "t", "slug": "t", "purpose": "p"}],
        )


def test_scaffold_produces_runnable_ejectable_project(tmp_path):
    plan = heuristic_plan("Local bakery with a blog", "Crumb")
    dest = tmp_path / "crumb"
    files = scaffold(plan, dest)
    names = {p.relative_to(dest).as_posix() for p in files}
    assert {
        "main.py",
        "seed.py",
        ".env.example",
        ".redblue.yml",
        "Dockerfile",
        ".github/workflows/redblue.yml",
        "content/plan.json",
        "tests/test_site.py",
        "README.md",
        "CLAUDE.md",
    } <= names
    assert "pull_request_target" not in (dest / ".github/workflows/redblue.yml").read_text()
    assert plan_from_project(dest).site_name == "Crumb"
    with pytest.raises(ScaffoldError):
        scaffold(plan, dest)
    r = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
        cwd=dest,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert r.returncode == 0, r.stdout + r.stderr


def test_seeded_drafts_cannot_publish_until_written(tmp_path):
    from redblue.cms import service
    from redblue.core.auth import Role, create_user
    from redblue.core.db import Database
    from redblue.growth.quality import quality_problems

    db = Database("sqlite://")
    db.create_all()
    with db.session() as s:
        admin = create_user(s, "a@example.com", None, Role.admin)
        plan = heuristic_plan("Consulting studio", "Studio")
        assert seed_from_plan(s, plan, admin) >= 4
        home = service.live_entry(s, "page", "home")
        assert home is None  # drafts only
        from sqlalchemy import select

        from redblue.cms.models import Entry

        draft = s.scalar(select(Entry).where(Entry.slug == "home"))
        assert any("TODO" in p for p in quality_problems(s, draft))
        assert json.loads(json.dumps(draft.seo))["target_questions"]


def test_generated_workflows_pin_actions_to_commits():
    import re
    from pathlib import Path

    from redblue.builder.scaffold import TEMPLATE_ROOT

    repo = Path(__file__).resolve().parents[3]
    workflows = list((TEMPLATE_ROOT / ".github" / "workflows").glob("*.yml"))
    workflows += list((repo / ".github" / "workflows").glob("*.yml"))
    workflows += [repo / "action" / "action.yml"]
    for wf in workflows:
        for ref in re.findall(r"uses:\s*([^\s#]+)", wf.read_text()):
            if ref.startswith("./"):
                continue
            assert re.search(r"@[0-9a-f]{40}$", ref), f"{wf.name}: {ref} is not SHA-pinned"
