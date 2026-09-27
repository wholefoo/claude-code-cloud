"""`redblue` CLI: new, dev, worker, doctor, gate, build, growth, users, backup."""

from __future__ import annotations

import os
import shutil
import sys
import time
from pathlib import Path

import typer

app = typer.Typer(help="RedBlue: build, secure and grow FastAPI sites.", no_args_is_help=True)
build_app = typer.Typer(help="Builder agent.", no_args_is_help=True)
growth_app = typer.Typer(help="Growth engine.", no_args_is_help=True)
app.add_typer(build_app, name="build")
app.add_typer(growth_app, name="growth")
video_app = typer.Typer(help="Trending video pipeline (needs redblue-video).", no_args_is_help=True)
app.add_typer(video_app, name="video")


def _settings():
    from redblue.core.config import get_settings

    return get_settings()


def _db():
    from redblue.core.db import Database

    db = Database(_settings().database_url)
    db.create_all()
    return db


def _platform():
    from redblue.core.app import build_platform

    return build_platform(_settings())


# ---------------------------------------------------------------- project


@app.command()
def new(
    name: str,
    intent: str = typer.Option("", help="Describe the site you want."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Approve the plan without asking."),
):
    """Plan and scaffold a new site (the plan is shown for approval first)."""
    from redblue.builder.plan import make_plan
    from redblue.builder.scaffold import ScaffoldError, scaffold

    intent = intent or typer.prompt("Describe the site (audience, what it offers, pages)")
    ai = _platform().ai if os.environ.get("ANTHROPIC_API_KEY") else None
    plan = make_plan(intent, ai, site_name=name.replace("-", " ").title())
    typer.echo("\n" + plan.summary() + "\n")
    if not yes and not typer.confirm("Create this site?", default=True):
        raise typer.Exit(1)
    dest = Path(name)
    try:
        scaffold(plan, dest)
    except ScaffoldError as exc:
        typer.secho(str(exc), fg="red")
        raise typer.Exit(1) from exc
    typer.secho(f"Created {dest}/", fg="green")
    typer.echo(
        f"Next:\n  cd {dest}\n"
        "  RB_ADMIN_EMAIL=you@example.com RB_ADMIN_PASSWORD='a long password' "
        "python seed.py\n  redblue dev"
    )


@app.command()
def dev(host: str = "127.0.0.1", port: int = 8000, reload: bool = True):
    """Run the site locally (uses ./main.py when present)."""
    import uvicorn

    target = "main:app" if Path("main.py").exists() else "redblue.platform.app:app"
    sys.path.insert(0, os.getcwd())
    uvicorn.run(target, host=host, port=port, reload=reload, proxy_headers=True)


@app.command()
def worker(once: bool = typer.Option(False, help="Process due jobs once and exit.")):
    """Run background jobs: scheduled publishing, growth reports, uptime checks, scans."""
    from redblue.platform.app import create_app

    sys.path.insert(0, os.getcwd())
    application = create_app()
    jobs = application.state.rb.jobs
    typer.echo(f"Worker started ({len(jobs.handlers)} job types).")
    while True:
        jobs.tick_schedules()
        n = jobs.drain(100)
        if once:
            typer.echo(f"Processed {n} job(s).")
            return
        time.sleep(5 if n == 0 else 0)


@app.command()
def createadmin(
    email: str,
    password: str = typer.Option(..., prompt=True, hide_input=True, confirmation_prompt=True),
):
    """Create (or fetch) an admin user."""
    from redblue.platform.seed import ensure_admin

    with _db().session() as s:
        ensure_admin(s, email, password)
    typer.secho(f"Admin {email} ready.", fg="green")


@app.command()
def seed(
    email: str = typer.Option(..., envvar="RB_ADMIN_EMAIL"),
    password: str = typer.Option(..., envvar="RB_ADMIN_PASSWORD"),
):
    """Create an admin and publish the starter content (demo sites)."""
    from redblue.platform.seed import ensure_admin
    from redblue.platform.seed import seed as run_seed

    with _db().session() as s:
        n = run_seed(s, ensure_admin(s, email, password))
    typer.echo(f"Seeded {n} entries.")


@app.command()
def migrate():
    """Create any missing tables (run after upgrading RedBlue)."""
    _db()
    typer.secho("Database is up to date.", fg="green")


@app.command()
def backup(dest: Path = typer.Option(Path("backups"))):
    """Back up the database and media directory."""
    s = _settings()
    dest.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    if s.database_url.startswith("sqlite:///"):
        src = Path(s.database_url.removeprefix("sqlite:///"))
        import sqlite3

        with sqlite3.connect(src) as con, sqlite3.connect(dest / f"db-{stamp}.sqlite3") as out:
            con.backup(out)
    else:
        typer.echo('For Postgres run: pg_dump "$DATABASE_URL" > backups/db-$(date +%F).sql')
    if s.storage_backend == "local" and Path(s.storage_dir).exists():
        shutil.make_archive(str(dest / f"media-{stamp}"), "gztar", s.storage_dir)
    typer.secho(f"Backup written to {dest}/", fg="green")


@app.command()
def doctor():
    """Check configuration, dependencies and security settings."""
    s = _settings()
    ok = True

    def check(label: str, passed: bool, hint: str = "", warn: bool = False):
        nonlocal ok
        mark = "✓" if passed else ("!" if warn else "✗")
        color = "green" if passed else ("yellow" if warn else "red")
        typer.secho(f" {mark} {label}" + ("" if passed else f": {hint}"), fg=color)
        if not passed and not warn:
            ok = False

    typer.echo(f"RedBlue doctor (env={s.env}, base_url={s.base_url})")
    check("Python ≥ 3.11", sys.version_info >= (3, 11), "upgrade Python")
    try:
        db = _db()
        with db.session() as sess:
            from sqlalchemy import text

            sess.execute(text("SELECT 1"))
        check("Database reachable", True)
    except Exception as exc:  # noqa: BLE001
        check("Database reachable", False, str(exc)[:200])
    try:
        Path(s.storage_dir).mkdir(parents=True, exist_ok=True)
        probe = Path(s.storage_dir) / ".rb-write-test"
        probe.write_text("ok")
        probe.unlink()
        check("Media storage writable", True)
    except OSError as exc:
        check("Media storage writable", s.storage_backend != "local", str(exc))
    for problem in s.validate_for_production():
        check("Production setting", False, problem, warn=not s.is_prod)
    check(
        "Email provider configured",
        s.email_provider != "console" or not s.is_prod,
        "set RB_EMAIL_PROVIDER=smtp for production",
        warn=True,
    )
    check(
        "Claude API key (optional, BYOK)",
        bool(s.anthropic_api_key),
        "agents run in deterministic mode",
        warn=True,
    )
    for tool in ("bandit", "semgrep", "pip-audit", "zap-baseline.py", "nuclei"):
        check(f"Gate scanner: {tool}", shutil.which(tool) is not None, "not installed", warn=True)
    if Path(".redblue.yml").exists():
        from redblue.gate.config import ConfigError, load_config

        try:
            load_config(None, ".")
            check(".redblue.yml valid", True)
        except ConfigError as exc:
            check(".redblue.yml valid", False, str(exc)[:300])
    typer.echo("")
    typer.secho(
        "All required checks passed." if ok else "Some checks failed.", fg="green" if ok else "red"
    )
    raise typer.Exit(0 if ok else 1)


@app.command(context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
def gate(ctx: typer.Context):
    """Security gate (see `redblue gate --help`): scan, fix, ci."""
    from redblue.gate.cli import main as gate_main

    raise typer.Exit(gate_main(ctx.args or ["--help"]))


@app.command()
def templates():
    """List the page-type catalog."""
    from redblue.templates.registry import CATEGORIES, by_category

    for key, items in by_category().items():
        typer.secho(CATEGORIES[key], bold=True)
        for pt in items:
            typer.echo(f"  {pt.key:20} {pt.name:32} {pt.schema_type}")


# ---------------------------------------------------------------- builder


@build_app.command("plan")
def build_plan(intent: str, name: str = ""):
    """Show the plan the builder would use for an idea."""
    from redblue.builder.plan import make_plan

    ai = _platform().ai if os.environ.get("ANTHROPIC_API_KEY") else None
    typer.echo(make_plan(intent, ai, site_name=name or None).summary())


@build_app.command("feature")
def build_feature(request: str, project: Path = Path(".")):
    """Implement a change on a new branch; tests and the gate must pass."""
    from redblue.builder.agent import BuilderAgent

    platform = _platform()
    result = BuilderAgent(platform.ai, platform.settings.models.builder).implement(project, request)
    for line in result.log:
        typer.echo(f"  {line}")
    typer.secho(
        f"{'Ready for review' if result.done else 'Needs work'}: branch {result.branch}",
        fg="green" if result.done else "yellow",
    )


# ---------------------------------------------------------------- growth


@growth_app.command("report")
def growth_report():
    """Generate the weekly growth report."""
    from redblue.growth.agent import GrowthAgent

    platform = _platform()
    with platform.db.session() as s:
        report = GrowthAgent(platform).weekly_report(s)
    typer.secho(report.headline, bold=True)
    for r in report.recommendations:
        typer.echo(f"  [{r.priority}] {r.title}: {r.why}")


@growth_app.command("audit")
def growth_audit():
    """Crawl the site in-process and audit SEO/AEO basics."""
    from fastapi.testclient import TestClient

    from redblue.cms.collections import get_collection
    from redblue.cms.service import live_entries
    from redblue.growth.audit import audit_site
    from redblue.platform.app import create_app

    application = create_app()
    s = application.state.rb.settings
    with application.state.rb.db.session() as db:
        paths = ["/"] + [
            get_collection(e.collection).path_for(e.live_slug, e.locale, s.default_locale)
            for e in live_entries(db, None, limit=500)
        ]
    with TestClient(application, base_url=s.base_url) as client:
        report = audit_site(client, list(dict.fromkeys(paths)))
    typer.secho(
        f"Score {report.score} · {len(report.pages)} pages · {len(report.issues)} issues", bold=True
    )
    for i in report.issues:
        typer.echo(f"  {i.severity:8} {i.path:40} {i.check}: {i.message}")


# ---------------------------------------------------------------- video


@video_app.command("templates")
def video_templates():
    """List video templates and output formats."""
    from redblue.video.templates import FORMATS, TEMPLATES

    typer.secho("Templates", bold=True)
    for t in TEMPLATES.values():
        typer.echo(f"  {t.key:8} {t.description}")
    typer.secho("Formats", bold=True)
    for f in FORMATS.values():
        typer.echo(f"  {f.key:5} {f.width}x{f.height}  {f.label}")


def _video():
    from redblue.video.config import get_video_settings
    from redblue.video.pipeline import Pipeline

    return Pipeline(_platform(), get_video_settings())


@video_app.command("sweep")
def video_sweep(limit: int = 15):
    """Fetch and score trends (YouTube Data API + Tavily)."""
    pipe = _video()
    with pipe.platform.db.session() as s:
        added = pipe.sweep(s)
        for t in sorted(added, key=lambda t: -t.score)[:limit]:
            typer.echo(f"  #{t.id:<5} {t.score:5.1f}  [{t.source}] {t.title[:80]}")
    typer.echo(f"{len(added)} new trend(s). Make one with: redblue video make --trend ID")


@video_app.command("make")
def video_make(
    topic: str = typer.Option("", help="Topic to research"),
    trend: int = typer.Option(0, help="Trend id from `redblue video sweep`"),
    template: str = typer.Option("", help="bold, clean, news or minimal"),
    fmt: list[str] = typer.Option([], "--format", "-f", help="9:16, 4:5, 1:1 or 16:9; repeatable"),
):
    """Research, script and render a video, then stop for human review in the admin."""
    from redblue.video.models import Trend

    pipe = _video()
    with pipe.platform.db.session() as s:
        t = s.get(Trend, trend) if trend else None
        p = pipe.start_project(s, trend=t, topic=topic or None)
        if template or fmt:
            pipe.set_look(p, template or pipe.s.template, list(fmt) or pipe.s.formats)
        try:
            pipe.run(s, p)
        except Exception as exc:  # noqa: BLE001
            typer.secho(f"Render failed: {exc}", fg="red")
        if p.problems:
            typer.secho("Fix in the admin before rendering:", fg="yellow")
            for x in p.problems:
                typer.echo(f"  - {x}")
        typer.echo(
            f"Project #{p.id}: {p.status}" + (f" -> {p.render_path}" if p.render_path else "")
        )
        typer.echo(f"Review at {pipe.platform.settings.base_url}/admin/video/projects/{p.id}")


if __name__ == "__main__":
    app()
