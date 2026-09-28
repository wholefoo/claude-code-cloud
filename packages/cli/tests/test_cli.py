from typer.testing import CliRunner

from redblue.cli.main import app

runner = CliRunner()


def test_new_scaffolds_project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    r = runner.invoke(
        app, ["new", "bakery-site", "--intent", "A local bakery with a blog", "--yes"]
    )
    assert r.exit_code == 0, r.output
    assert (tmp_path / "bakery-site" / "main.py").exists()
    assert "Blog" in r.output


def test_doctor_and_templates(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RB_DATABASE_URL", f"sqlite:///{tmp_path}/d.db")
    monkeypatch.setenv("RB_STORAGE_DIR", str(tmp_path / "m"))
    from redblue.core.config import get_settings

    get_settings.cache_clear()
    r = runner.invoke(app, ["doctor"])
    assert "Database reachable" in r.output and r.exit_code == 0, r.output
    r = runner.invoke(app, ["templates"])
    assert "blog_post" in r.output and "Answer & knowledge" in r.output
    get_settings.cache_clear()


def test_gate_passthrough(tmp_path):
    (tmp_path / "ok.py").write_text("x = 1\n")
    r = runner.invoke(app, ["gate", "scan", "--path", str(tmp_path), "--no-ai"])
    assert r.exit_code == 0 and "Gate passed" in r.output


def test_db_commands(tmp_path, monkeypatch):
    monkeypatch.setenv("RB_DATABASE_URL", f"sqlite:///{tmp_path}/cli.db")
    runner = CliRunner()
    assert "unversioned" in runner.invoke(app, ["db", "current"]).output
    r = runner.invoke(app, ["db", "upgrade"])
    assert r.exit_code == 0 and "0001_baseline (was unversioned)" in r.output
    r = runner.invoke(app, ["db", "check"])
    assert r.exit_code == 0 and "Models and migrations match." in r.output
    r = runner.invoke(app, ["db", "revision", "-m", "nothing"])
    assert r.exit_code == 0 and "No model changes" in r.output
