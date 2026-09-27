"""Pipeline orchestration: sweep → pick → brief → script → assets → render → human review.

Every stage degrades gracefully without API keys (so the loop can be tried end to end), and
every stage records what it did on the project. Nothing is ever published automatically:
approval only marks a render as ready for a human to upload."""

from __future__ import annotations

import logging
import re
from pathlib import Path

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from redblue.core.context import Platform
from redblue.core.db import utcnow
from redblue.video import clients, performance
from redblue.video.captions import Transcriber, align, get_transcriber
from redblue.video.config import VideoSettings
from redblue.video.models import Trend, VideoProject
from redblue.video.render import BeatMedia, media_duration, render
from redblue.video.schemas import Asset, Brief, Script, Source, TrendSignal
from redblue.video.scoring import score_all
from redblue.video.templates import Format, Template, get_format, get_template
from redblue.video.writing import make_brief, make_script

log = logging.getLogger("redblue.video")


class Pipeline:
    def __init__(
        self,
        platform: Platform,
        settings: VideoSettings,
        http: httpx.Client | None = None,
        transcriber: Transcriber | None = None,
    ):
        self.platform, self.s, self.http = platform, settings, http
        self._transcriber = transcriber

    @property
    def model(self) -> str:
        return self.platform.settings.models.content

    # ------------------------------------------------------------------ 1. sweep

    def sweep(self, db: Session) -> list[Trend]:
        signals: list[TrendSignal] = []
        notes = []
        sources = (
            ("youtube", self._youtube),
            ("tavily", self._tavily),
            ("reddit", self._reddit),
            ("google_trends", self._google_trends),
        )
        for name, fn in sources:
            try:
                signals += fn()
            except clients.MissingKey as exc:
                notes.append(str(exc))
            except (httpx.HTTPError, ValueError) as exc:
                notes.append(f"{name} failed: {exc}")
        for n in notes:
            log.info("sweep: %s", n)
        seen = {t for (t,) in db.execute(select(Trend.url).where(Trend.url.is_not(None)))}
        added = []
        fb = performance.feedback(db, self.s)  # your track record, once there's enough data
        scored = score_all(
            signals,
            self.s.niche,
            self.s.keywords,
            feedback=fb,
            weight_overrides=self.s.score_weights,
        )
        for sig, score, parts in scored:
            if sig.url and sig.url in seen:
                continue
            t = Trend(
                source=sig.source,
                title=sig.title,
                topic=_topic(sig.title),
                url=sig.url,
                signal=sig.model_dump(mode="json"),
                score=score,
                breakdown=parts,
            )
            db.add(t)
            added.append(t)
        db.flush()
        return added

    def _youtube(self) -> list[TrendSignal]:
        yt = clients.YouTubeTrends(self.s.key("youtube"), self.http)
        return yt.search(self.s.niche) + yt.most_popular(self.s.region, limit=15)

    def _tavily(self) -> list[TrendSignal]:
        return clients.Tavily(self.s.key("tavily"), self.http).trend_signals(self.s.niche)

    def _reddit(self) -> list[TrendSignal]:
        if not self.s.subreddits:
            raise clients.MissingKey("Set RB_VIDEO_SUBREDDITS to read Reddit.")
        reddit = clients.Reddit(self.s.reddit_credentials, self.s.reddit_user_agent, self.http)
        return reddit.trend_signals(self.s.subreddits)

    def _google_trends(self) -> list[TrendSignal]:
        geo = self.s.google_trends_geo or self.s.region
        return clients.GoogleTrends(self.http).trending(geo)

    # ------------------------------------------------------------------ 2. project + brief

    def start_project(
        self, db: Session, *, trend: Trend | None = None, topic: str | None = None
    ) -> VideoProject:
        if trend is not None:
            trend.status = "picked"
        p = VideoProject(
            trend_id=trend.id if trend else None,
            topic=(topic or (trend.topic if trend else "")).strip()[:300],
        )
        if not p.topic:
            raise ValueError("A project needs a trend or a topic.")
        db.add(p)
        db.flush()
        return p

    def research(self, db: Session, p: VideoProject) -> Brief:
        sources = self._sources(p.topic)
        brief = make_brief(p.topic, sources, self.platform.ai, self.model)
        p.brief, p.status = brief.model_dump(mode="json"), "script"
        return brief

    def _sources(self, topic: str) -> list[Source]:
        try:
            sources = clients.Tavily(self.s.key("tavily"), self.http).sources(topic)
        except (clients.MissingKey, httpx.HTTPError) as exc:
            log.info("research: %s", exc)
            return []
        try:
            fc = clients.Firecrawl(self.s.key("firecrawl"), self.http)
        except clients.MissingKey:
            return sources
        for s in sources[:3]:  # deepen the top sources with full-page extraction
            try:
                text = fc.scrape(s.url)
                if len(text) > len(s.content):
                    s.content = text[:12000]
            except (httpx.HTTPError, ValueError) as exc:
                log.info("firecrawl %s: %s", s.url, exc)
        return sources

    # ------------------------------------------------------------------ 3. script

    def write_script(self, db: Session, p: VideoProject) -> Script:
        brief = Brief.model_validate(p.brief)
        script = make_script(brief, self.platform.ai, self.model, self.s.target_seconds)
        self.save_script(p, script)
        return script

    def save_script(self, p: VideoProject, script: Script) -> list[str]:
        problems = script.validate_against(Brief.model_validate(p.brief))
        p.script, p.problems = script.model_dump(mode="json"), problems
        p.status = "script"
        return problems

    # ------------------------------------------------------------------ 4+5. assets + render

    def produce(self, db: Session, p: VideoProject) -> Path:
        script = Script.model_validate(p.script)
        if p.problems:
            raise ValueError("Fix the script problems before rendering: " + "; ".join(p.problems))
        tpl, fmts = self.look(p)
        workdir = (self.s.output_dir / f"project-{p.id}").resolve()
        workdir.mkdir(parents=True, exist_ok=True)
        media, assets = self._assets(script, workdir, fmts[0].orientation)
        self._time_words(script, media)
        p.status = "rendering"
        renders: dict[str, str] = {}
        for fmt in fmts:  # same script, footage and narration; each format framed separately
            out = render(
                script, media, workdir / f"video-{p.id}-{fmt.slug}.mp4", fmt=fmt, template=tpl
            )
            renders[fmt.key] = str(out)
        p.assets = [a.model_dump(mode="json") for a in assets]
        p.renders = renders
        p.render_path, p.status = renders[fmts[0].key], "review"
        return Path(p.render_path)

    def look(self, p: VideoProject) -> tuple[Template, list[Format]]:
        """The project's template and formats, falling back to the site settings."""
        tpl = get_template(p.template or self.s.template)
        keys = p.formats or self.s.formats or ["9:16"]
        fmts = list(dict.fromkeys(get_format(k) for k in keys))
        return tpl, fmts

    def set_look(self, p: VideoProject, template: str, formats: list[str]) -> None:
        tpl = get_template(template)
        fmts = [get_format(f).key for f in formats] or ["9:16"]
        p.template, p.formats = tpl.key, list(dict.fromkeys(fmts))

    def _assets(
        self, script: Script, workdir: Path, orientation: str = "portrait"
    ) -> tuple[list[BeatMedia], list[Asset]]:
        media, assets = [], []
        try:
            pexels = clients.Pexels(self.s.key("pexels"), self.http)
        except clients.MissingKey:
            pexels = None
        try:
            tts = clients.ElevenLabs(self.s.key("elevenlabs"), self.s.voice_id, self.http)
        except clients.MissingKey:
            tts = None
        for i, beat in enumerate(script.beats):
            m = BeatMedia()
            if pexels:
                try:
                    hit = pexels.find(beat.visual_query, orientation)
                    if hit:
                        dest = workdir / f"clip-{i:02d}.mp4"
                        pexels.download(hit["url"], dest)
                        m.clip = dest
                        assets.append(
                            Asset(
                                kind="video",
                                path=str(dest),
                                provider="pexels",
                                license="Pexels License",
                                source_url=hit["page"],
                                attribution=f"Video by {hit['author']} on Pexels",
                            )
                        )
                except (httpx.HTTPError, ValueError) as exc:
                    log.info("pexels beat %s: %s", i, exc)
            if tts:
                try:
                    dest = workdir / f"voice-{i:02d}.mp3"
                    tts.speak(beat.narration, dest)
                    m.narration = dest
                    assets.append(
                        Asset(
                            kind="audio",
                            path=str(dest),
                            provider="elevenlabs",
                            license="ElevenLabs terms (AI voice)",
                            attribution="AI-generated narration",
                        )
                    )
                except httpx.HTTPError as exc:
                    log.info("tts beat %s: %s", i, exc)
            media.append(m)
        return media, assets

    def transcriber(self) -> Transcriber | None:
        if self._transcriber is None and self.s.captions == "whisper":
            self._transcriber = get_transcriber("whisper", self.s.whisper_model)
            if self._transcriber is None:  # don't retry the load for every beat
                self.s = self.s.model_copy(update={"captions": "even"})
        return self._transcriber

    def _time_words(self, script: Script, media: list[BeatMedia]) -> None:
        """Word-level caption timing from the narration audio (Whisper, local)."""
        if not any(m.narration for m in media):
            return
        whisper = self.transcriber()
        if whisper is None:
            return
        for beat, m in zip(script.beats, media, strict=True):
            if not m.narration:
                continue
            try:
                heard = whisper.words(m.narration)
            except Exception as exc:  # noqa: BLE001 - captions degrade, render continues
                log.info("whisper failed on %s: %s", m.narration.name, exc)
                continue
            m.words = align(beat.narration, heard, media_duration(m.narration) or beat.seconds)

    # ------------------------------------------------------------------ 6. review

    def review(self, p: VideoProject, user_id: int, approve: bool, note: str = "") -> None:
        if p.status != "review":
            raise ValueError("Only rendered videos can be reviewed.")
        p.status = "approved" if approve else "rejected"
        p.review_note, p.reviewed_by = note[:2000], user_id

    # ------------------------------------------------------------------ 7. performance

    def track(self, db: Session) -> performance.TrackResult:
        """Snapshot stats for published videos and check pending TikTok/Instagram uploads.
        Read-only: nothing is ever posted or published from here."""
        from redblue.video.upload_social import refresh_pending  # avoids an import cycle

        res = performance.track(db, self.s, self.http)
        res.notes += refresh_pending(db, self.s, self.http)
        return res

    def run(self, db: Session, p: VideoProject) -> VideoProject:
        """Brief → script → render in one go (stops at script problems for a human)."""
        if not p.brief:
            self.research(db, p)
        if not p.script:
            self.write_script(db, p)
        if not p.problems:
            try:
                self.produce(db, p)
            except Exception as exc:
                p.status, p.problems = "failed", [f"Render failed: {exc}"[:500]]
                raise
        p.updated_at = utcnow()
        return p


def description(p: VideoProject) -> str:
    """Upload description with sources and asset credits (for the human who publishes)."""
    script = Script.model_validate(p.script) if p.script else None
    brief = Brief.model_validate(p.brief) if p.brief else None
    lines = [script.title if script else p.topic, ""]
    if brief and brief.sources:
        lines.append("Sources:")
        lines += [f"[{i}] {s.title}: {s.url}" for i, s in enumerate(brief.sources)]
        lines.append("")
    credits = [a["attribution"] for a in p.assets if a.get("attribution")]
    if credits:
        lines.append("Credits: " + "; ".join(dict.fromkeys(credits)))
    if p.ai_generated:
        lines.append("Made with AI assistance (script, voice) and reviewed by a human.")
    if script and script.hashtags:
        lines.append(" ".join("#" + h.lstrip("#") for h in script.hashtags))
    return "\n".join(lines)


def _topic(title: str) -> str:
    title = re.sub(r"\s*[|\-–—:]\s*[^|\-–—:]{0,40}$", "", title)  # drop "| Channel" suffixes
    return re.sub(r"[#@]\w+", "", title).strip()[:300] or title[:300]
