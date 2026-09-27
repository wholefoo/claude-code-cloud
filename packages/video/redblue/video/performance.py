"""Performance tracking: record where a human published each video, collect its numbers
(YouTube APIs, CSV exports, or typed in), compare videos fairly, and learn which trend
sources and topics do well for *you* so the next sweep ranks them accordingly.

Comparisons are made at the same age (views at ``perf_window_hours``, interpolated between
snapshots) and within a platform (lift = views vs. your median there), because a 3-day-old
Short and a 3-week-old TikTok can't be compared by raw counts. Effects are averaged in log
space and shrunk toward zero, so one lucky video doesn't swing the scores.

Reading your own stats is all this does: nothing here posts, comments or uploads."""

from __future__ import annotations

import csv
import io
import math
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from statistics import median
from urllib.parse import parse_qs, urlsplit, urlunsplit

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from redblue.core.db import utcnow
from redblue.video import clients
from redblue.video.config import VideoSettings
from redblue.video.models import MetricSnapshot, Publication, Trend, VideoProject
from redblue.video.scoring import WEIGHTS, Feedback, tokens
from redblue.video.templates import get_format

PLATFORMS: dict[str, tuple[str, ...]] = {
    "youtube": ("youtube.com", "youtu.be"),
    "tiktok": ("tiktok.com",),
    "instagram": ("instagram.com",),
    "facebook": ("facebook.com", "fb.watch"),
    "linkedin": ("linkedin.com",),
    "x": ("x.com", "twitter.com"),
    "threads": ("threads.net", "threads.com"),
    "pinterest": ("pinterest.com", "pin.it"),
    "snapchat": ("snapchat.com",),  # recorded by hand (no posting API)
    "reddit": ("reddit.com", "redd.it"),
    "bluesky": ("bsky.app",),
    "tumblr": ("tumblr.com",),
    "vimeo": ("vimeo.com",),
    "twitch": ("twitch.tv",),  # recorded by hand (no upload API)
    "dailymotion": ("dailymotion.com", "dai.ly"),
    "rumble": ("rumble.com",),
    "other": (),
}
MIN_PEERS = 3  # mature videos on a platform before lift (vs. your median) is meaningful
PRIOR = 2.0  # shrinkage: an effect needs several videos to move far from zero
MIN_GROUP = 2  # a source/word needs this many videos before it's used for scoring
MAX_CSV_ROWS = 2000
_YT_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")


# ---------------------------------------------------------------------- publications


def _host_matches(host: str, domains: tuple[str, ...]) -> bool:
    return any(host == d or host.endswith("." + d) for d in domains)


def youtube_id(url: str) -> str | None:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if host == "youtu.be" or host.endswith(".youtu.be"):
        candidate = parts.path.strip("/").split("/")[0]
    elif _host_matches(host, ("youtube.com",)):
        segs = [s for s in parts.path.split("/") if s]
        if segs[:1] == ["watch"]:
            candidate = (parse_qs(parts.query).get("v") or [""])[0]
        elif len(segs) >= 2 and segs[0] in ("shorts", "live", "embed", "v"):
            candidate = segs[1]
        else:
            return None
    else:
        return None
    return candidate if _YT_ID.match(candidate) else None


def classify(url: str, platform: str | None = None) -> tuple[str, str, str | None]:
    """Validate a published-video URL → ``(platform, normalized_url, external_id)``."""
    url = (url or "").strip()
    if not url or len(url) > 1000:
        raise ValueError("Enter the video's URL.")
    parts = urlsplit(url)
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        raise ValueError("The URL must start with https://")
    if parts.username or parts.password:
        raise ValueError("The URL must not contain credentials.")
    host = parts.hostname.lower()
    detected = next((k for k, d in PLATFORMS.items() if d and _host_matches(host, d)), "other")
    platform = (platform or "").strip().lower() or detected
    if platform not in PLATFORMS:
        raise ValueError(f"Unknown platform {platform!r}.")
    if platform != "other" and platform != detected:
        raise ValueError(f"That URL isn't a {platform} link.")
    if platform == "youtube":
        vid = youtube_id(url)
        if not vid:
            raise ValueError("Couldn't find the YouTube video id in that URL.")
        return platform, f"https://www.youtube.com/watch?v={vid}", vid
    host = re.sub(r"^(www|m)\.", "", host)  # one canonical form, so CSV rows match
    norm = urlunsplit(("https", host, parts.path.rstrip("/"), parts.query, ""))
    return platform, norm, None


def record(
    db: Session,
    project: VideoProject,
    url: str,
    *,
    platform: str | None = None,
    format: str | None = None,
    published_at: datetime | None = None,
) -> Publication:
    """A human says "I uploaded this video here". Only approved videos can be recorded."""
    if project.status != "approved":
        raise ValueError("Approve the video before recording where it was published.")
    platform, norm, ext = classify(url, platform)
    if db.scalar(select(Publication.id).where(Publication.url == norm)):
        raise ValueError("That URL is already recorded.")
    fmt = get_format(format).key if format else None
    pub = Publication(
        project_id=project.id,
        platform=platform,
        url=norm,
        external_id=ext,
        format=fmt,
        published_at=_naive(published_at) if published_at else utcnow(),
    )
    db.add(pub)
    db.flush()
    return pub


def add_snapshot(
    db: Session,
    pub: Publication,
    *,
    source: str,
    views: int,
    likes: int | None = None,
    comments: int | None = None,
    shares: int | None = None,
    avg_view_pct: float | None = None,
    avg_view_seconds: float | None = None,
    taken_at: datetime | None = None,
) -> MetricSnapshot:
    counts = (views, likes, comments, shares, avg_view_seconds)
    if any(v is not None and v < 0 for v in counts):
        raise ValueError("Counts can't be negative.")
    if avg_view_pct is not None and not 0 <= avg_view_pct <= 1000:
        raise ValueError("Average view percentage must be between 0 and 1000.")
    snap = MetricSnapshot(
        publication_id=pub.id,
        source=source[:20],
        taken_at=_naive(taken_at) if taken_at else utcnow(),
        views=int(views),
        likes=likes,
        comments=comments,
        shares=shares,
        avg_view_pct=avg_view_pct,
        avg_view_seconds=avg_view_seconds,
    )
    db.add(snap)
    db.flush()
    return snap


def _naive(dt: datetime) -> datetime:
    return dt.astimezone(UTC).replace(tzinfo=None) if dt.tzinfo else dt


# ---------------------------------------------------------------------- collecting


@dataclass
class TrackResult:
    updated: int = 0
    notes: list[str] = field(default_factory=list)


def track(db: Session, s: VideoSettings, http: httpx.Client | None = None) -> TrackResult:
    """Snapshot stats for recent YouTube publications (other platforms: CSV or by hand)."""
    res = TrackResult()
    since = utcnow() - timedelta(days=s.track_days)
    pubs = list(
        db.scalars(
            select(Publication).where(
                Publication.platform == "youtube",
                Publication.external_id.is_not(None),
                Publication.published_at >= since,
            )
        )
    )
    if not pubs:
        return res
    ids = [p.external_id for p in pubs if p.external_id]
    public: dict[str, dict] = {}
    deep: dict[str, dict] = {}
    try:
        public = clients.YouTubeStats(s.key("youtube"), http).stats(ids)
    except clients.MissingKey as exc:
        res.notes.append(str(exc))
    except httpx.HTTPError as exc:
        res.notes.append(f"YouTube stats failed: {exc}")
    try:
        oldest = min(p.published_at for p in pubs)
        deep = clients.YouTubeAnalytics(s.youtube_oauth, http).stats(ids, oldest)
    except clients.MissingKey:
        pass  # optional: retention needs OAuth
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        res.notes.append(f"YouTube Analytics failed: {exc}")
    for p in pubs:
        pub_row, deep_row = public.get(p.external_id or ""), deep.get(p.external_id or "")
        if not pub_row and not deep_row:
            continue
        if pub_row and pub_row.get("published_at"):
            p.published_at = _naive(pub_row["published_at"])
        base = pub_row or deep_row or {}
        extra = deep_row or {}
        add_snapshot(
            db,
            p,
            source="youtube_api" if pub_row else "youtube_analytics",
            # Public counts are real-time; Analytics lags, so prefer the larger view count.
            views=max(base.get("views") or 0, extra.get("views") or 0),
            likes=base.get("likes") if base.get("likes") is not None else extra.get("likes"),
            comments=base.get("comments")
            if base.get("comments") is not None
            else extra.get("comments"),
            shares=extra.get("shares"),
            avg_view_pct=extra.get("avg_view_pct"),
            avg_view_seconds=extra.get("avg_view_seconds"),
        )
        res.updated += 1
    return res


_CSV_ALIASES = {
    "url": ("url", "link", "video_url", "video_link", "permalink"),
    "views": ("views", "video_views", "plays", "view_count", "impressions_views"),
    "likes": ("likes", "like_count", "reactions"),
    "comments": ("comments", "comment_count"),
    "shares": ("shares", "share_count"),
    "avg_view_pct": (
        "avg_view_pct",
        "average_view_percentage",
        "average_percentage_viewed",
        "average_percentage_viewed_(%)",
        "avg_watch_pct",
    ),
    "avg_view_seconds": ("avg_view_seconds", "average_view_duration", "average_watch_time"),
    "taken_at": ("taken_at", "date", "as_of"),
}


def _header(name: str) -> str:
    return re.sub(r"\s+", "_", name.strip().lower().lstrip("﻿"))


def _num(value: str | None, kind=int):
    if value is None:
        return None
    value = value.strip().replace(",", "").rstrip("%")
    if not value:
        return None
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"not a number: {value[:20]!r}")
    return kind(number)


def import_csv(db: Session, text: str) -> tuple[int, list[str]]:
    """Import counts from a CSV (one row per video, matched by URL). Returns
    ``(imported, errors)``. Only URLs already recorded as publications are accepted."""
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        return 0, ["The CSV is empty."]
    cols: dict[str, str] = {}
    for raw in reader.fieldnames:
        h = _header(raw or "")
        for key, names in _CSV_ALIASES.items():
            if h in names and key not in cols:
                cols[key] = raw
    if "url" not in cols or "views" not in cols:
        return 0, ["The CSV needs a url column and a views column."]
    imported, errors = 0, []
    for n, row in enumerate(reader, start=2):
        if n - 1 > MAX_CSV_ROWS:
            errors.append(f"Stopped after {MAX_CSV_ROWS} rows.")
            break
        get = {k: row.get(c) for k, c in cols.items()}
        try:
            _, norm, _ = classify(get["url"] or "")
            pub = db.scalar(select(Publication).where(Publication.url == norm))
            if pub is None:
                raise ValueError("URL isn't recorded on any project")
            taken = get.get("taken_at")
            add_snapshot(
                db,
                pub,
                source="csv",
                views=_num(get["views"]) or 0,
                likes=_num(get.get("likes")),
                comments=_num(get.get("comments")),
                shares=_num(get.get("shares")),
                avg_view_pct=_num(get.get("avg_view_pct"), float),
                avg_view_seconds=_num(get.get("avg_view_seconds"), float),
                taken_at=datetime.fromisoformat(taken.strip()) if taken and taken.strip() else None,
            )
            imported += 1
        except ValueError as exc:
            errors.append(f"Row {n}: {str(exc)[:120]}")
    return imported, errors[:50]


# ---------------------------------------------------------------------- analysis


def views_at(pub: Publication, snaps: list[MetricSnapshot], hours: float) -> float | None:
    """Views at ``hours`` after publishing, interpolated between snapshots; None until a
    snapshot at least that old exists (the video isn't mature yet)."""
    points = [(0.0, 0.0)] + sorted(
        ((s.taken_at - pub.published_at).total_seconds() / 3600, float(s.views)) for s in snaps
    )
    if points[-1][0] < hours:
        return None
    for (h0, v0), (h1, v1) in zip(points, points[1:], strict=False):
        if h0 <= hours <= h1:
            return v0 if h1 == h0 else v0 + (v1 - v0) * (hours - h0) / (h1 - h0)
    return None


@dataclass
class PubRow:
    pub: Publication
    project: VideoProject
    latest: MetricSnapshot | None
    window_views: float | None
    log_lift: float | None  # log2 vs. the platform median at the same age

    @property
    def lift(self) -> float | None:
        return None if self.log_lift is None else 2**self.log_lift

    @property
    def engagement(self) -> float | None:
        s = self.latest
        if not s or not s.views:
            return None
        return ((s.likes or 0) + 2 * (s.comments or 0) + 3 * (s.shares or 0)) / s.views


@dataclass
class Group:
    feature: str
    value: str
    n: int
    effect: float  # shrunk mean log2 lift

    @property
    def multiplier(self) -> float:
        return 2**self.effect


@dataclass
class Report:
    rows: list[PubRow]
    outcomes: dict[int, float]  # project id → mean log2 lift
    groups: dict[str, list[Group]]
    feedback: Feedback
    active: bool  # enough mature videos for feedback to be applied to scoring
    correlations: dict[str, float | None]
    medians: dict[str, float]
    window_hours: int
    min_videos: int


def _shrunk(values: list[float]) -> float:
    return sum(values) / (len(values) + PRIOR)


def spearman(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) < 5 or len(xs) != len(ys):
        return None

    def ranks(v: list[float]) -> list[float]:
        order = sorted(range(len(v)), key=lambda i: v[i])
        r = [0.0] * len(v)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and v[order[j + 1]] == v[order[i]]:
                j += 1
            for k in range(i, j + 1):
                r[order[k]] = (i + j) / 2
            i = j + 1
        return r

    rx, ry = ranks(xs), ranks(ys)
    mx, my = sum(rx) / len(rx), sum(ry) / len(ry)
    cov = sum((a - mx) * (b - my) for a, b in zip(rx, ry, strict=True))
    vx = math.sqrt(sum((a - mx) ** 2 for a in rx))
    vy = math.sqrt(sum((b - my) ** 2 for b in ry))
    return None if vx == 0 or vy == 0 else round(cov / (vx * vy), 2)


def report(db: Session, s: VideoSettings) -> Report:
    pubs = list(db.scalars(select(Publication).order_by(Publication.published_at.desc())))
    snaps: dict[int, list[MetricSnapshot]] = defaultdict(list)
    for snap in db.scalars(select(MetricSnapshot).order_by(MetricSnapshot.taken_at)):
        snaps[snap.publication_id].append(snap)
    projects = {p.id: p for p in db.scalars(select(VideoProject))}
    trend_ids = [p.trend_id for p in projects.values() if p.trend_id]
    trends = {t.id: t for t in db.scalars(select(Trend).where(Trend.id.in_(trend_ids)))}

    hours = s.perf_window_hours
    window = {p.id: views_at(p, snaps[p.id], hours) for p in pubs}
    by_platform: dict[str, list[float]] = defaultdict(list)
    for p in pubs:
        if window[p.id] is not None:
            by_platform[p.platform].append(window[p.id])
    medians = {k: median(v) for k, v in by_platform.items() if len(v) >= MIN_PEERS}

    rows = []
    for p in pubs:
        wv, med = window[p.id], medians.get(p.platform)
        log_lift = None
        if wv is not None and med is not None:
            log_lift = max(-3.0, min(3.0, math.log2((wv + 1) / (med + 1))))
        rows.append(PubRow(p, projects[p.project_id], (snaps[p.id] or [None])[-1], wv, log_lift))

    per_project: dict[int, list[float]] = defaultdict(list)
    for r in rows:
        if r.log_lift is not None:
            per_project[r.project.id].append(r.log_lift)
    outcomes = {pid: sum(v) / len(v) for pid, v in per_project.items()}

    niche_words = tokens(s.niche + " " + " ".join(s.keywords))
    feats: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for pid, o in outcomes.items():
        proj = projects[pid]
        trend = trends.get(proj.trend_id) if proj.trend_id else None
        feats["source"][trend.source if trend else "topic"].append(o)
        feats["template"][proj.template or s.template].append(o)
        for w in tokens(proj.topic) - niche_words:
            feats["word"][w].append(o)
    for r in rows:
        if r.log_lift is not None and r.pub.format:
            feats["format"][r.pub.format].append(r.log_lift)
    groups = {
        f: sorted(
            (Group(f, k, len(v), _shrunk(v)) for k, v in vals.items()),
            key=lambda g: (-g.effect, -g.n),
        )
        for f, vals in feats.items()
    }

    fb = Feedback(
        sources={
            g.value: g.effect
            for g in groups.get("source", [])
            if g.n >= MIN_GROUP and g.value != "topic"
        },
        words={g.value: g.effect for g in groups.get("word", []) if g.n >= MIN_GROUP},
    )
    active = s.learn_from_performance and len(outcomes) >= s.learn_min_videos and bool(fb)

    scored = [
        (trends[projects[pid].trend_id], o)
        for pid, o in outcomes.items()
        if projects[pid].trend_id in trends
    ]
    xs = {k: [t.breakdown.get(k, 0.0) for t, _ in scored] for k in WEIGHTS}
    xs["score"] = [t.score for t, _ in scored]
    outs = [o for _, o in scored]
    correlations = {k: spearman(v, outs) for k, v in xs.items()}
    return Report(
        rows, outcomes, groups, fb, active, correlations, medians, hours, s.learn_min_videos
    )


def feedback(db: Session, s: VideoSettings) -> Feedback | None:
    """The scoring feedback to apply on the next sweep, or None if there isn't enough data."""
    if not s.learn_from_performance:
        return None
    r = report(db, s)
    return r.feedback if r.active else None
