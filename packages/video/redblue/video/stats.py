"""Automatic numbers for published videos on platforms other than YouTube.

For each recent publication, read its counts from the platform's API with the credentials
already set for uploading, and store a snapshot. Only your own posts are read, with GET
requests (TikTok's query endpoint is a POST that reads); nothing is ever posted.

Not covered: Reddit, Bluesky and Tumblr report no view counts, and LinkedIn, Rumble,
Snapchat and Twitch have no suitable API. X bills each read, so it's opt-in
(``RB_VIDEO_X_STATS=true``). Use a CSV or type numbers in for those."""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from datetime import timedelta

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from redblue.core.db import utcnow
from redblue.video import clients, performance
from redblue.video import upload_social as social
from redblue.video.config import VideoSettings
from redblue.video.models import Publication

log = logging.getLogger("redblue.video.stats")

# Where each platform's own id sits in a recorded (normalized) URL.
_ID_IN_URL = {
    "tiktok": re.compile(r"^https://(?:[a-z]+\.)?tiktok\.com/@[^/]+/video/(\d{5,30})$"),
    "facebook": re.compile(
        r"^https://(?:[a-z]+\.)?facebook\.com/(?:reel/|[^/]+/videos/|watch/?\?v=)(\d{5,30})"
    ),
    "x": re.compile(r"^https://(?:x|twitter)\.com/[^/]+/status/(\d{5,30})$"),
    "pinterest": re.compile(r"^https://(?:[a-z]+\.)?pinterest\.com/pin/(\d{5,30})$"),
    "vimeo": re.compile(r"^https://vimeo\.com/(?:.*/)?(\d{5,20})$"),
    "dailymotion": re.compile(r"^https://dailymotion\.com/video/(x[0-9a-z]{2,20})$"),
}
NEEDS = {
    "tiktok": "TIKTOK_CLIENT_KEY, TIKTOK_CLIENT_SECRET and TIKTOK_REFRESH_TOKEN (scope video.list)",
    "instagram": "INSTAGRAM_ACCESS_TOKEN and INSTAGRAM_USER_ID (insights permission)",
    "facebook": "FACEBOOK_PAGE_ACCESS_TOKEN and FACEBOOK_PAGE_ID (read_insights)",
    "threads": "THREADS_ACCESS_TOKEN and THREADS_USER_ID (threads_manage_insights)",
    "pinterest": "the Pinterest credentials and RB_VIDEO_PINTEREST_BOARD_ID (pins:read)",
    "vimeo": "VIMEO_ACCESS_TOKEN",
    "dailymotion": "DAILYMOTION_API_KEY, DAILYMOTION_API_SECRET and DAILYMOTION_CHANNEL_ID",
    "x": "RB_VIDEO_X_STATS=true and the X credentials (X bills each read)",
}


def configured(s: VideoSettings) -> dict[str, bool]:
    return {
        "tiktok": s.tiktok_credentials is not None,
        "instagram": s.instagram_credentials is not None,
        "facebook": s.facebook_credentials is not None,
        "threads": s.threads_credentials is not None,
        "pinterest": s.pinterest_credentials is not None and bool(s.pinterest_board_id),
        "vimeo": s.vimeo_access_token is not None,
        "dailymotion": s.dailymotion_credentials is not None and bool(s.dailymotion_channel_id),
        "x": s.x_stats and s.x_credentials is not None,
    }


def _ids_from_urls(pubs: list[Publication], platform: str) -> None:
    pattern = _ID_IN_URL[platform]
    for p in pubs:
        if not p.external_id and (m := pattern.match(p.url)):
            p.external_id = m.group(1)


def _ids_from_listing(pubs: list[Publication], listing: Callable[[], dict[str, str]]) -> None:
    """Instagram/Threads links carry a shortcode, not the API id: match recent posts."""
    missing = [p for p in pubs if not p.external_id]
    if not missing:
        return
    by_url = {}
    for link, media_id in listing().items():
        try:
            by_url[performance.classify(link)[1]] = media_id
        except ValueError:
            continue
    for p in missing:
        p.external_id = by_url.get(p.url)


def _one_by_one(pubs, fetch, errors: list[str]) -> dict[str, dict]:
    """One request per video; a video that fails (deleted, not yours) doesn't stop the rest."""
    out = {}
    for p in pubs:
        if not p.external_id:
            continue
        try:
            out[p.external_id] = fetch(p.external_id)
        except (clients.PlatformError, ValueError) as exc:
            errors.append(str(exc))
    return out


def _fetch(
    platform: str, pubs: list[Publication], s: VideoSettings, http, errors: list[str]
) -> dict[str, dict]:
    if platform == "tiktok":
        _ids_from_urls(pubs, platform)
        ids = [p.external_id for p in pubs if p.external_id]
        return social.tiktok_client(s, http).video_stats(ids) if ids else {}
    if platform == "x":
        _ids_from_urls(pubs, platform)
        ids = [p.external_id for p in pubs if p.external_id]
        return social.x_client(s, http).post_stats(ids) if ids else {}
    if platform == "instagram":
        ig = social.instagram_client(s, http)
        _ids_from_listing(pubs, ig.recent_media)
        return _one_by_one(pubs, ig.media_stats, errors)
    if platform == "threads":
        th = social.threads_client(s, http)
        _ids_from_listing(pubs, th.recent_media)
        return _one_by_one(pubs, th.media_stats, errors)
    _ids_from_urls(pubs, platform)
    if platform == "facebook":
        return _one_by_one(pubs, social.facebook_client(s, http).video_stats, errors)
    if platform == "vimeo":
        return _one_by_one(pubs, social.vimeo_client(s, http).video_stats, errors)
    if platform == "dailymotion":
        return _one_by_one(pubs, social.dailymotion_client(s, http).video_stats, errors)
    if platform == "pinterest":
        pin = social.pinterest_client(s, http)
        since = {p.external_id: p.published_at.date() for p in pubs if p.external_id}
        return _one_by_one(pubs, lambda pid: pin.pin_stats(pid, since[pid]), errors)
    raise ValueError(f"No stats API for {platform}.")


def track(
    db: Session, s: VideoSettings, http: httpx.Client | None = None
) -> performance.TrackResult:
    """Snapshot counts for recent publications on every configured platform. Read-only."""
    res = performance.TrackResult()
    since = utcnow() - timedelta(days=s.track_days)
    ready = configured(s)
    for platform in NEEDS:
        pubs = list(
            db.scalars(
                select(Publication).where(
                    Publication.platform == platform, Publication.published_at >= since
                )
            )
        )
        if not pubs:
            continue
        if not ready[platform]:
            res.notes.append(
                f"{social.name(platform)}: {len(pubs)} recent video(s); set {NEEDS[platform]} "
                "to fetch their numbers, or import a CSV."
            )
            continue
        errors: list[str] = []
        try:
            counts = _fetch(platform, pubs, s, http, errors)
        except (clients.MissingKey, clients.PlatformError, httpx.HTTPError, ValueError) as exc:
            res.notes.append(f"{social.name(platform)} stats failed: {exc}"[:300])
            continue
        unmatched = 0
        for p in pubs:
            row = counts.get(p.external_id or "")
            if not row or not isinstance(row.get("views"), int):
                unmatched += 1
                continue
            performance.add_snapshot(
                db,
                p,
                source=f"{platform}_api",
                views=row["views"],
                likes=row.get("likes"),
                comments=row.get("comments"),
                shares=row.get("shares"),
            )
            res.updated += 1
        if unmatched:
            why = f" First error: {errors[0]}" if errors else ""
            res.notes.append(
                f"{social.name(platform)}: no numbers for {unmatched} video(s) "
                f"(not found on your account, or views are hidden).{why}"[:300]
            )
    return res
