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


@video_app.command("publish")
def video_publish(
    project: int = typer.Argument(..., help="Project id (must be approved)"),
    url: str = typer.Argument(..., help="Where you uploaded it"),
    platform: str = typer.Option("", help="youtube, tiktok, instagram, ... (default: detect)"),
    fmt: str = typer.Option("", "--format", "-f", help="Which render: 9:16, 4:5, 1:1, 16:9"),
):
    """Record where you published a video, so its performance can be tracked."""
    from redblue.video import performance
    from redblue.video.models import VideoProject

    pipe = _video()
    with pipe.platform.db.session() as s:
        p = s.get(VideoProject, project)
        if p is None:
            raise typer.BadParameter(f"No project #{project}")
        try:
            pub = performance.record(s, p, url, platform=platform or None, format=fmt or None)
        except ValueError as exc:
            raise typer.BadParameter(str(exc)) from None
        typer.echo(f"Recorded #{pub.id} on {pub.platform}: {pub.url}")


@video_app.command("track")
def video_track():
    """Fetch stats for published YouTube videos (read-only)."""
    pipe = _video()
    with pipe.platform.db.session() as s:
        res = pipe.track(s)
    for n in res.notes:
        typer.secho(n, fg="yellow")
    typer.echo(f"Updated {res.updated} video(s).")


@video_app.command("import-stats")
def video_import_stats(path: Path = typer.Argument(..., exists=True, dir_okay=False)):
    """Import numbers from a CSV (url, views, likes, comments, shares, avg_view_pct, date)."""
    from redblue.video import performance

    pipe = _video()
    with pipe.platform.db.session() as s:
        n, errors = performance.import_csv(s, path.read_text(encoding="utf-8-sig"))
    for e in errors:
        typer.secho(f"  {e}", fg="yellow")
    typer.echo(f"Imported {n} row(s).")


@video_app.command("performance")
def video_performance():
    """How published videos did, and what the next sweep will learn from it."""
    from redblue.video import performance

    pipe = _video()
    with pipe.platform.db.session() as s:
        r = performance.report(s, pipe.s)
        typer.secho(f"Published videos (views at {r.window_hours} h vs. your median)", bold=True)
        for row in r.rows:
            views = row.latest.views if row.latest else 0
            window = f"{row.window_views:,.0f}" if row.window_views is not None else "too early"
            lift = f"x{row.lift:.2f}" if row.lift is not None else "-"
            typer.echo(
                f"  #{row.project.id:<4} {row.pub.platform:9} {views:>10,} views  "
                f"@{r.window_hours}h {window:>10}  {lift:>6}  {row.project.topic[:50]}"
            )
        state = "on" if r.active else f"off (needs {r.min_videos} mature videos)"
        typer.secho(f"Scoring feedback: {state}", bold=True)
        for feature in ("source", "word", "template", "format"):
            groups = r.groups.get(feature, [])
            if feature == "word":
                groups = [g for g in groups if g.n >= 2]  # words seen once aren't used
            for g in groups[:8]:
                typer.echo(f"  {feature:8} {g.value:20} n={g.n:<3} x{g.multiplier:.2f}")


@video_app.command("upload")
def video_upload(
    project: int = typer.Argument(..., help="Approved project id"),
    fmt: str = typer.Option("9:16", "--format", "-f", help="Which render to upload"),
    privacy: str = typer.Option("private", help="private, unlisted or public"),
    made_for_kids: bool = typer.Option(
        ..., "--made-for-kids/--not-made-for-kids", help="Required: YouTube audience setting"
    ),
    title: str = typer.Option("", help="Defaults to the script title"),
):
    """Upload an approved render to YouTube. Always asks you to confirm; there is no --yes."""
    import getpass

    import httpx

    from redblue.video import upload as uploads
    from redblue.video.models import VideoProject

    pipe = _video()
    _need_person()
    with pipe.platform.db.session() as s:
        p = s.get(VideoProject, project)
        if p is None:
            raise typer.BadParameter(f"No project #{project}")
        d = uploads.defaults(p, pipe.s)
        try:
            req = uploads.UploadRequest(
                format=fmt,
                title=title or d["title"] or p.topic,
                description=d["description"],
                tags=d["tags"],
                privacy=privacy,
                made_for_kids=made_for_kids,
                category_id=d["category_id"],
            )
            path = uploads.render_file(p, req.format, pipe.s)
        except ValueError as exc:
            raise typer.BadParameter(str(exc)) from None
        typer.secho(f"Title:   {req.title}", bold=True)
        typer.echo(f"File:    {path}\nPrivacy: {req.privacy}  ·  made for kids: {made_for_kids}")
        typer.echo("Description:\n  " + req.description.replace("\n", "\n  "))
        confirmed = typer.confirm("I watched this render and have the rights to it. Upload?")
        public_ok = req.privacy != "public" or typer.confirm(
            "It will be PUBLIC immediately. Continue?"
        )
        if not (confirmed and public_ok):
            raise typer.Abort()
        try:
            pub = uploads.upload(
                s,
                p,
                req,
                pipe.s,
                confirmed_by=f"cli:{getpass.getuser()}",
                confirmed=confirmed,
                confirmed_public=public_ok,
            )
        except (ValueError, uploads.UploadDisabled, httpx.HTTPError) as exc:
            typer.secho(f"Upload failed: {exc}", fg="red")
            raise typer.Exit(1) from None
        typer.echo(f"Uploaded ({pub.privacy}): {pub.url}")


def _need_person() -> None:
    if not sys.stdin.isatty():
        raise typer.BadParameter("Uploading needs a person at the keyboard to confirm it.")


@video_app.command("tiktok")
def video_tiktok(
    project: int = typer.Argument(..., help="Approved project id"),
    fmt: str = typer.Option("9:16", "--format", "-f", help="Which render to send"),
    privacy: str = typer.Option("", help="Direct mode only: one of the account's options"),
):
    """Send an approved render to TikTok (drafts by default). Always asks you to confirm."""
    import getpass

    import httpx

    from redblue.video import upload_social as social
    from redblue.video.models import VideoProject

    pipe = _video()
    _need_person()
    with pipe.platform.db.session() as s:
        p = s.get(VideoProject, project)
        if p is None:
            raise typer.BadParameter(f"No project #{project}")
        direct = pipe.s.tiktok_mode == "direct"
        try:
            if direct:
                opts = social.tiktok_options(pipe.s)
                typer.echo(
                    f"Posting as {opts['nickname']}. Privacy options: " + ", ".join(opts["privacy"])
                )
                if not privacy:
                    raise typer.BadParameter("Choose --privacy from the options above.")
            req = social.TikTokRequest(
                format=fmt,
                caption=social.defaults(p)["tiktok_caption"] if direct else "",
                privacy=privacy or None if direct else None,
            )
        except (ValueError, social.clients.PlatformError, httpx.HTTPError) as exc:
            raise typer.BadParameter(str(exc)) from None
        where = f"post it ({req.privacy})" if direct else "send it to your TikTok drafts"
        confirmed = typer.confirm(f"I watched this render and have the rights to it. {where}?")
        public_ok = (
            not direct
            or req.privacy == "SELF_ONLY"
            or typer.confirm("Other people will be able to see it. Continue?")
        )
        if not (confirmed and public_ok):
            raise typer.Abort()
        try:
            up = social.upload_tiktok(
                s,
                p,
                req,
                pipe.s,
                confirmed_by=f"cli:{getpass.getuser()}",
                confirmed=confirmed,
                confirmed_public=public_ok,
            )
        except (
            ValueError,
            social.UploadDisabled,
            social.clients.PlatformError,
            httpx.HTTPError,
        ) as exc:
            typer.secho(f"Upload failed: {exc}", fg="red")
            raise typer.Exit(1) from None
        typer.echo(f"Upload #{up.id} sent ({up.mode}). Check it with: redblue video uploads")


@video_app.command("instagram")
def video_instagram(
    project: int = typer.Argument(..., help="Approved project id"),
    fmt: str = typer.Option("9:16", "--format", "-f", help="Which render to upload"),
):
    """Upload an approved render as an Instagram Reel (not public until you publish it)."""
    import getpass

    import httpx

    from redblue.video import upload_social as social
    from redblue.video.models import VideoProject

    pipe = _video()
    _need_person()
    with pipe.platform.db.session() as s:
        p = s.get(VideoProject, project)
        if p is None:
            raise typer.BadParameter(f"No project #{project}")
        try:
            req = social.InstagramRequest(
                format=fmt, caption=social.defaults(p)["instagram_caption"]
            )
        except ValueError as exc:
            raise typer.BadParameter(str(exc)) from None
        typer.echo("Caption:\n  " + req.caption.replace("\n", "\n  "))
        if not typer.confirm("I watched this render and have the rights to it. Upload?"):
            raise typer.Abort()
        try:
            up = social.upload_instagram(
                s, p, req, pipe.s, confirmed_by=f"cli:{getpass.getuser()}", confirmed=True
            )
        except (
            ValueError,
            social.UploadDisabled,
            social.clients.PlatformError,
            httpx.HTTPError,
        ) as exc:
            typer.secho(f"Upload failed: {exc}", fg="red")
            raise typer.Exit(1) from None
        typer.echo(
            f"Upload #{up.id} is processing (not public). "
            f"Publish it with: redblue video instagram-publish {up.id}"
        )


@video_app.command("uploads")
def video_uploads():
    """List TikTok/Instagram uploads and check their status (read-only)."""
    import httpx
    from sqlalchemy import select

    from redblue.video import upload_social as social
    from redblue.video.models import Upload

    pipe = _video()
    with pipe.platform.db.session() as s:
        for up in s.scalars(select(Upload).order_by(Upload.id.desc()).limit(30)):
            try:
                social.refresh(s, up, pipe.s)
            except (
                social.clients.MissingKey,
                social.clients.PlatformError,
                httpx.HTTPError,
            ) as exc:
                typer.secho(f"  #{up.id}: status check failed: {exc}", fg="yellow")
            typer.echo(
                f"  #{up.id:<4} project {up.project_id:<4} {up.platform:9} {up.format:5} "
                f"{up.status:10} {up.url or ''}"
            )


@video_app.command("instagram-publish")
def video_instagram_publish(upload: int = typer.Argument(..., help="Upload id")):
    """Make an uploaded Reel public on Instagram. Always asks you to confirm."""
    import getpass

    import httpx

    from redblue.video import upload_social as social
    from redblue.video.models import Upload

    pipe = _video()
    _need_person()
    with pipe.platform.db.session() as s:
        up = s.get(Upload, upload)
        if up is None:
            raise typer.BadParameter(f"No upload #{upload}")
        if not typer.confirm("Make this Reel PUBLIC on Instagram now?"):
            raise typer.Abort()
        try:
            social.publish_instagram(
                s, up, pipe.s, confirmed_by=f"cli:{getpass.getuser()}", confirmed=True
            )
        except (
            ValueError,
            social.UploadDisabled,
            social.clients.PlatformError,
            httpx.HTTPError,
        ) as exc:
            typer.secho(f"Publish failed: {exc}", fg="red")
            raise typer.Exit(1) from None
        typer.echo(f"Published: {up.url or '(link pending)'}")


@video_app.command("facebook")
def video_facebook(
    project: int = typer.Argument(..., help="Approved project id"),
    fmt: str = typer.Option("9:16", "--format", "-f", help="Which render to upload"),
    publish_now: bool = typer.Option(False, "--publish-now", help="Default: save as a draft"),
):
    """Upload an approved render as a Facebook Page Reel (a draft unless --publish-now)."""
    import getpass

    import httpx

    from redblue.video import upload_social as social
    from redblue.video.models import VideoProject

    pipe = _video()
    _need_person()
    with pipe.platform.db.session() as s:
        p = s.get(VideoProject, project)
        if p is None:
            raise typer.BadParameter(f"No project #{project}")
        d = social.defaults(p)
        try:
            req = social.FacebookRequest(
                format=fmt,
                title=d["title"],
                description=d["facebook_description"],
                publish_now=publish_now,
            )
        except ValueError as exc:
            raise typer.BadParameter(str(exc)) from None
        where = "publish it on your Page now" if publish_now else "save it as a Page draft"
        confirmed = typer.confirm(f"I watched this render and have the rights to it. {where}?")
        public_ok = not publish_now or typer.confirm("It will be PUBLIC right away. Continue?")
        if not (confirmed and public_ok):
            raise typer.Abort()
        try:
            up = social.upload_facebook(
                s,
                p,
                req,
                pipe.s,
                confirmed_by=f"cli:{getpass.getuser()}",
                confirmed=confirmed,
                confirmed_public=public_ok,
            )
        except (
            ValueError,
            social.UploadDisabled,
            social.clients.PlatformError,
            httpx.HTTPError,
        ) as exc:
            typer.secho(f"Upload failed: {exc}", fg="red")
            raise typer.Exit(1) from None
        typer.echo(f"Upload #{up.id} sent ({up.mode}). Check it with: redblue video uploads")


@video_app.command("linkedin")
def video_linkedin(
    project: int = typer.Argument(..., help="Approved project id"),
    fmt: str = typer.Option("16:9", "--format", "-f", help="Which render to upload"),
):
    """Upload an approved render to LinkedIn (not posted until you run linkedin-publish)."""
    import getpass

    import httpx

    from redblue.video import upload_social as social
    from redblue.video.models import VideoProject

    pipe = _video()
    _need_person()
    with pipe.platform.db.session() as s:
        p = s.get(VideoProject, project)
        if p is None:
            raise typer.BadParameter(f"No project #{project}")
        d = social.defaults(p)
        try:
            req = social.LinkedInRequest(
                format=fmt, title=d["title"], commentary=d["linkedin_commentary"]
            )
        except ValueError as exc:
            raise typer.BadParameter(str(exc)) from None
        typer.echo("Post text:\n  " + req.commentary.replace("\n", "\n  "))
        if not typer.confirm("I watched this render and have the rights to it. Upload?"):
            raise typer.Abort()
        try:
            up = social.upload_linkedin(
                s, p, req, pipe.s, confirmed_by=f"cli:{getpass.getuser()}", confirmed=True
            )
        except (
            ValueError,
            social.UploadDisabled,
            social.clients.PlatformError,
            httpx.HTTPError,
        ) as exc:
            typer.secho(f"Upload failed: {exc}", fg="red")
            raise typer.Exit(1) from None
        typer.echo(
            f"Upload #{up.id} is processing (not posted). Post it with: "
            f"redblue video linkedin-publish {up.id} --visibility PUBLIC"
        )


@video_app.command("linkedin-publish")
def video_linkedin_publish(
    upload: int = typer.Argument(..., help="Upload id"),
    visibility: str = typer.Option(..., help="Required: PUBLIC or CONNECTIONS"),
):
    """Create the LinkedIn post for an uploaded video. Always asks you to confirm."""
    import getpass

    import httpx

    from redblue.video import upload_social as social
    from redblue.video.models import Upload

    pipe = _video()
    _need_person()
    with pipe.platform.db.session() as s:
        up = s.get(Upload, upload)
        if up is None:
            raise typer.BadParameter(f"No upload #{upload}")
        if not typer.confirm(f"Post this on LinkedIn now ({visibility.upper()})?"):
            raise typer.Abort()
        try:
            social.publish_linkedin(
                s,
                up,
                pipe.s,
                visibility=visibility.upper(),
                confirmed_by=f"cli:{getpass.getuser()}",
                confirmed=True,
            )
        except (
            ValueError,
            social.UploadDisabled,
            social.clients.PlatformError,
            httpx.HTTPError,
        ) as exc:
            typer.secho(f"Publish failed: {exc}", fg="red")
            raise typer.Exit(1) from None
        typer.echo(f"Posted: {up.url}")


def _social_step1(platform: str, project: int, fmt: str, text_key: str, **extra):
    """Shared CLI flow for upload-then-post platforms (X, Threads)."""
    import getpass

    import httpx

    from redblue.video import upload_social as social
    from redblue.video.models import VideoProject

    pipe = _video()
    _need_person()
    with pipe.platform.db.session() as s:
        p = s.get(VideoProject, project)
        if p is None:
            raise typer.BadParameter(f"No project #{project}")
        text = social.defaults(p)[text_key]
        model = social.XRequest if platform == "x" else social.ThreadsRequest
        try:
            req = model(format=fmt, text=text)
        except ValueError as exc:
            raise typer.BadParameter(str(exc)) from None
        typer.echo(f"Post text: {req.text}")
        if not typer.confirm("I watched this render and have the rights to it. Upload?"):
            raise typer.Abort()
        fn = social.upload_x if platform == "x" else social.upload_threads
        if platform == "threads":
            extra = {
                "public_base": pipe.s.public_base_url or pipe.platform.settings.base_url,
                "secret": pipe.platform.settings.secret_key.get_secret_value(),
            }
        try:
            up = fn(
                s, p, req, pipe.s, confirmed_by=f"cli:{getpass.getuser()}", confirmed=True, **extra
            )
        except (
            ValueError,
            social.UploadDisabled,
            social.clients.PlatformError,
            httpx.HTTPError,
        ) as exc:
            typer.secho(f"Upload failed: {exc}", fg="red")
            raise typer.Exit(1) from None
        typer.echo(
            f"Upload #{up.id} is processing (not posted). Post it with: "
            f"redblue video {platform}-publish {up.id}"
        )


def _social_step2(platform: str, upload: int):
    import getpass

    import httpx

    from redblue.video import upload_social as social
    from redblue.video.models import Upload

    pipe = _video()
    _need_person()
    with pipe.platform.db.session() as s:
        up = s.get(Upload, upload)
        if up is None or up.platform != platform:
            raise typer.BadParameter(f"No {social.name(platform)} upload #{upload}")
        question = (
            "Pin this to your Pinterest board now?"
            if platform == "pinterest"
            else f"Post this PUBLICLY on {social.name(platform)} now?"
        )
        if not typer.confirm(question):
            raise typer.Abort()
        try:
            social.publish(s, up, pipe.s, confirmed_by=f"cli:{getpass.getuser()}", confirmed=True)
        except (
            ValueError,
            social.UploadDisabled,
            social.clients.PlatformError,
            httpx.HTTPError,
        ) as exc:
            typer.secho(f"Publish failed: {exc}", fg="red")
            raise typer.Exit(1) from None
        typer.echo(f"Posted: {up.url or '(link pending)'}")


@video_app.command("x")
def video_x(
    project: int = typer.Argument(..., help="Approved project id"),
    fmt: str = typer.Option("16:9", "--format", "-f", help="Which render to upload"),
):
    """Upload an approved render to X (not posted until you run x-publish)."""
    _social_step1("x", project, fmt, "x_text")


@video_app.command("x-publish")
def video_x_publish(upload: int = typer.Argument(..., help="Upload id")):
    """Post an uploaded video on X. Always asks you to confirm."""
    _social_step2("x", upload)


@video_app.command("threads")
def video_threads(
    project: int = typer.Argument(..., help="Approved project id"),
    fmt: str = typer.Option("9:16", "--format", "-f", help="Which render to send"),
):
    """Send an approved render to Threads (not posted until you run threads-publish)."""
    _social_step1("threads", project, fmt, "threads_text")


@video_app.command("threads-publish")
def video_threads_publish(upload: int = typer.Argument(..., help="Upload id")):
    """Post an uploaded video on Threads. Always asks you to confirm."""
    _social_step2("threads", upload)


@video_app.command("pinterest")
def video_pinterest(
    project: int = typer.Argument(..., help="Approved project id"),
    fmt: str = typer.Option("9:16", "--format", "-f", help="Which render to upload"),
    link: str = typer.Option("", help="Optional https link for the Pin"),
):
    """Upload an approved render to Pinterest (not pinned until you run pinterest-publish)."""
    import getpass

    import httpx

    from redblue.video import upload_social as social
    from redblue.video.models import VideoProject

    pipe = _video()
    _need_person()
    with pipe.platform.db.session() as s:
        p = s.get(VideoProject, project)
        if p is None:
            raise typer.BadParameter(f"No project #{project}")
        d = social.defaults(p)
        try:
            req = social.PinterestRequest(
                format=fmt,
                title=d["title"][:100],
                description=d["pinterest_description"],
                link=link,
            )
        except ValueError as exc:
            raise typer.BadParameter(str(exc)) from None
        typer.echo(f"Title: {req.title}")
        if not typer.confirm("I watched this render and have the rights to it. Upload?"):
            raise typer.Abort()
        try:
            up = social.upload_pinterest(
                s, p, req, pipe.s, confirmed_by=f"cli:{getpass.getuser()}", confirmed=True
            )
        except (
            ValueError,
            social.UploadDisabled,
            social.clients.PlatformError,
            httpx.HTTPError,
        ) as exc:
            typer.secho(f"Upload failed: {exc}", fg="red")
            raise typer.Exit(1) from None
        typer.echo(
            f"Upload #{up.id} is processing (not pinned). "
            f"Pin it with: redblue video pinterest-publish {up.id}"
        )


@video_app.command("pinterest-publish")
def video_pinterest_publish(upload: int = typer.Argument(..., help="Upload id")):
    """Create the Pin for an uploaded video. Always asks you to confirm."""
    _social_step2("pinterest", upload)


if __name__ == "__main__":
    app()
