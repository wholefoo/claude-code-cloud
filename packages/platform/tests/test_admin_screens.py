"""Browser check of the admin: every page in the nav plus the video pages, at desktop and
phone widths, in real Chromium. Fails on server errors, console errors (including CSP
violations), horizontal overflow and broken upload cards; saves a full-page screenshot of
each page to ``RB_SCREENSHOT_DIR`` (default: the test's temp dir) for a person to look at.

Needs Playwright and a Chromium build (``pip install playwright`` and
``python -m playwright install chromium``, or ``RB_CHROMIUM_PATH`` pointing at a Chrome
binary); skipped otherwise, so the normal test run doesn't depend on a browser."""

from __future__ import annotations

import os
import re
import socket
import threading
import time
from pathlib import Path

import pytest

sync_api = pytest.importorskip("playwright.sync_api")

VIEWPORTS = {"desktop": (1280, 900), "phone": (390, 844)}
VIDEO = b"\x00\x00\x00\x18ftypmp42 screenshot video bytes"


def _browser(p):
    try:
        return p.chromium.launch()
    except sync_api.Error as exc:
        path = os.environ.get("RB_CHROMIUM_PATH")
        if not path:
            pytest.skip(f"No Chromium for Playwright ({str(exc).splitlines()[0][:120]})")
        return p.chromium.launch(executable_path=path)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def live_admin(tmp_path, monkeypatch):
    """The platform app with seeded content and a video project, served on a local port."""
    import uvicorn

    from redblue.core.config import Settings
    from redblue.core.security import limiter
    from redblue.platform.app import create_app
    from redblue.platform.seed import ensure_admin, seed
    from redblue.video import config as vconfig
    from redblue.video import performance
    from redblue.video.models import Upload, VideoProject

    monkeypatch.chdir(tmp_path)
    for k, v in {
        "RB_VIDEO_OUTPUT_DIR": str(tmp_path / "out"),
        "RB_VIDEO_UPLOAD_ENABLED": "true",
        "VIMEO_ACCESS_TOKEN": "not-a-real-token",
    }.items():
        monkeypatch.setenv(k, v)
    vconfig.get_video_settings.cache_clear()
    limiter.reset()
    port = _free_port()
    app = create_app(
        Settings(
            database_url=f"sqlite:///{tmp_path}/t.db",
            env="test",
            storage_dir=tmp_path / "media",
            secret_key="k" * 48,
            base_url=f"http://127.0.0.1:{port}",
            email_from="owner@example.com",
        )
    )
    vs = vconfig.get_video_settings()
    vs.output_dir.mkdir(parents=True, exist_ok=True)
    render = vs.output_dir / "video-1-16x9.mp4"
    render.write_bytes(VIDEO)
    with app.state.rb.db.session() as db:
        admin = ensure_admin(db, "admin@example.com", "correct-horse-battery")
        seed(db, admin)
        p = VideoProject(
            topic="Heat pumps explained",
            status="approved",
            renders={"16:9": str(render)},
            script={
                "title": "Heat pumps explained",
                "hook": "h",
                "cta": "c",
                "beats": [
                    {"narration": "a b", "visual_query": "q", "seconds": 3},
                    {"narration": "c d", "visual_query": "q", "seconds": 3},
                ],
            },
        )
        db.add(p)
        db.flush()
        db.add(
            Upload(
                project_id=p.id,
                platform="vimeo",
                format="16:9",
                mode="nobody",
                status="draft",
                external_ref="900001",
                privacy="nobody",
                uploaded_by="admin@example.com",
                meta={},
            )
        )
        yt = performance.record(db, p, "https://youtu.be/dQw4w9WgXcQ", format="16:9")
        performance.add_snapshot(db, yt, source="manual", views=12400, likes=800, comments=30)
        rd = performance.record(db, p, "https://www.reddit.com/r/energy/comments/1abcde/heat/")
        performance.add_snapshot(db, rd, source="reddit_api", views=None, likes=321, comments=45)
        pid = p.id

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    try:
        yield f"http://127.0.0.1:{port}", pid
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        vconfig.get_video_settings.cache_clear()


def _watch(page, problems: list[str]) -> None:
    page.on(
        "console",
        lambda m: (
            problems.append(f"console {m.type}: {m.text[:200]}") if m.type == "error" else None
        ),
    )
    page.on("pageerror", lambda e: problems.append(f"page error: {str(e)[:200]}"))


# The outermost element that sticks out past the window, e.g. "table.rb-table (588px)".
_WIDEST = """() => {
  const w = window.innerWidth;
  const inScroller = el => {  // content inside a sideways-scrolling box is fine
    for (let p = el.parentElement; p && p !== document.body; p = p.parentElement) {
      if (['auto', 'scroll', 'hidden'].includes(getComputedStyle(p).overflowX)) return true;
    }
    return false;
  };
  for (const el of document.querySelectorAll('body *')) {
    const r = el.getBoundingClientRect();
    if (r.right > w + 1 && !inScroller(el)) {
      const cls = el.className && typeof el.className === 'string'
        ? '.' + el.className.trim().split(/\\s+/).join('.') : '';
      return el.tagName.toLowerCase() + cls + ' (' + Math.round(r.right) + 'px)';
    }
  }
  return 'unknown element';
}"""


def _visit(page, url: str, shots: Path, name: str, problems: list[str]) -> None:
    r = page.goto(url)
    if r is None or r.status >= 400:
        problems.append(f"{name}: HTTP {r.status if r else 'no response'}")
        return
    page.wait_for_load_state("networkidle")
    width = page.evaluate("document.documentElement.scrollWidth")
    inner = page.evaluate("window.innerWidth")
    if width > inner + 1:
        culprit = page.evaluate(_WIDEST)
        problems.append(f"{name}: page is {width}px wide in a {inner}px window ({culprit})")
    page.screenshot(path=str(shots / f"{name}.png"), full_page=True)


def test_admin_pages_in_a_browser(live_admin, tmp_path):
    base, pid = live_admin
    shots = Path(os.environ.get("RB_SCREENSHOT_DIR") or tmp_path / "screens")
    shots.mkdir(parents=True, exist_ok=True)
    problems: list[str] = []
    project = f"/admin/video/projects/{pid}"
    with sync_api.sync_playwright() as p:
        browser = _browser(p)
        for label, (w, h) in VIEWPORTS.items():
            page = browser.new_page(viewport={"width": w, "height": h})
            _watch(page, problems)
            page.goto(f"{base}/admin/login")
            page.fill('input[name="email"]', "admin@example.com")
            page.fill('input[name="password"]', "correct-horse-battery")
            page.click('button[type="submit"]')
            page.wait_for_url(re.compile(r".*/admin/?$"))

            nav = page.eval_on_selector_all(
                '.rb-admin__nav a[href^="/admin"]', "els => els.map(e => e.getAttribute('href'))"
            )
            assert "/admin/video" in nav, nav
            paths = list(dict.fromkeys([*nav, project, "/admin/video/performance"]))
            for path in paths:
                name = (path.strip("/").replace("/", "-") or "admin") + f"-{label}"
                _visit(page, base + path, shots, name, problems)

            # Upload cards: collapsed until clicked; "Other platforms" lists the rest.
            page.goto(base + project)
            card = page.locator("details#upload-vimeo")
            assert card.get_attribute("open") is None
            assert "· sent" in card.locator("summary").inner_text()
            card.locator("summary").click()
            assert card.get_attribute("open") is not None
            assert page.locator("details#upload-setup summary").is_visible()
            assert page.get_by_text("no view count · 321 points · 45 comments").is_visible()

            # A failed upload comes back with its card open and scrolled into view.
            page.goto(f"{base}{project}?open=vimeo&msg=Vimeo%20upload%20failed#upload-vimeo")
            card = page.locator("details#upload-vimeo")
            assert card.get_attribute("open") is not None
            top = card.evaluate("e => e.getBoundingClientRect().top")
            assert -2 <= top < h, f"{label}: reopened card is at {top}px, outside the window"
            page.screenshot(path=str(shots / f"video-project-reopened-{label}.png"))
            page.close()
        browser.close()
    assert not problems, "\n".join(problems)
