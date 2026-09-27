"""Thin clients for trend, research and asset APIs. Each takes an optional httpx.Client so
tests (and proxies) can inject a transport. Only official APIs and the open web are used:
nothing here downloads other creators' videos."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import quote, urlsplit

import httpx
from defusedxml import ElementTree

from redblue.video.schemas import Source, TrendSignal

TIMEOUT = httpx.Timeout(30.0, connect=10.0)


class MissingKey(RuntimeError):
    pass


def _client(client: httpx.Client | None) -> httpx.Client:
    return client or httpx.Client(timeout=TIMEOUT, follow_redirects=False)


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


class YouTubeTrends:
    """YouTube Data API v3: most-popular chart and niche searches (metadata only)."""

    BASE = "https://www.googleapis.com/youtube/v3"

    def __init__(self, api_key: str | None, client: httpx.Client | None = None):
        if not api_key:
            raise MissingKey("Set YOUTUBE_API_KEY to read YouTube trends.")
        self.key, self.http = api_key, _client(client)

    def _videos(self, ids: list[str]) -> list[TrendSignal]:
        if not ids:
            return []
        r = self.http.get(
            f"{self.BASE}/videos",
            params={"part": "snippet,statistics", "id": ",".join(ids), "key": self.key},
        )
        r.raise_for_status()
        out = []
        for item in r.json().get("items", []):
            sn, st = item.get("snippet", {}), item.get("statistics", {})
            out.append(
                TrendSignal(
                    source="youtube",
                    title=sn.get("title", "")[:500],
                    url=f"https://www.youtube.com/watch?v={item['id']}",
                    topic=sn.get("title", "")[:300],
                    published_at=_parse_dt(sn.get("publishedAt")),
                    views=int(st.get("viewCount", 0) or 0),
                    likes=int(st.get("likeCount", 0) or 0),
                    comments=int(st.get("commentCount", 0) or 0),
                    snippet=(sn.get("description") or "")[:500],
                    extra={"channel": sn.get("channelTitle"), "tags": sn.get("tags", [])[:15]},
                )
            )
        return out

    def most_popular(
        self, region: str = "US", category_id: str | None = None, limit: int = 25
    ) -> list[TrendSignal]:
        params = {
            "part": "id",
            "chart": "mostPopular",
            "regionCode": region,
            "maxResults": min(limit, 50),
            "key": self.key,
        }
        if category_id:
            params["videoCategoryId"] = category_id
        r = self.http.get(f"{self.BASE}/videos", params=params)
        r.raise_for_status()
        return self._videos([i["id"] for i in r.json().get("items", [])])

    def search(self, query: str, days: int = 7, limit: int = 15) -> list[TrendSignal]:
        after = (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        r = self.http.get(
            f"{self.BASE}/search",
            params={
                "part": "id",
                "q": query,
                "type": "video",
                "order": "viewCount",
                "publishedAfter": after,
                "maxResults": min(limit, 50),
                "key": self.key,
            },
        )
        r.raise_for_status()
        return self._videos(
            [
                i["id"]["videoId"]
                for i in r.json().get("items", [])
                if i.get("id", {}).get("videoId")
            ]
        )


class YouTubeStats:
    """Public counts for *your own* uploads (YouTube Data API, API key, 1 quota unit per 50)."""

    BASE = YouTubeTrends.BASE

    def __init__(self, api_key: str | None, client: httpx.Client | None = None):
        if not api_key:
            raise MissingKey("Set YOUTUBE_API_KEY to fetch YouTube stats.")
        self.key, self.http = api_key, _client(client)

    def stats(self, ids: list[str]) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for i in range(0, len(ids), 50):
            r = self.http.get(
                f"{self.BASE}/videos",
                params={
                    "part": "snippet,statistics",
                    "id": ",".join(ids[i : i + 50]),
                    "key": self.key,
                },
            )
            r.raise_for_status()
            for item in r.json().get("items", []):
                st = item.get("statistics", {})
                out[item["id"]] = {
                    "views": _int(st.get("viewCount")) or 0,
                    "likes": _int(st.get("likeCount")),
                    "comments": _int(st.get("commentCount")),
                    "published_at": _parse_dt(item.get("snippet", {}).get("publishedAt")),
                }
        return out


class YouTubeAnalytics:
    """YouTube Analytics API, read-only (scope ``yt-analytics.readonly``): retention and
    shares, which public counts don't include. Needs an OAuth client and a refresh token for
    the channel owner; see the README. Data lags two to three days."""

    TOKEN_URL = "https://oauth2.googleapis.com/token"  # noqa: S105  # nosec B105
    REPORTS = "https://youtubeanalytics.googleapis.com/v2/reports"
    METRICS = "views,likes,comments,shares,averageViewDuration,averageViewPercentage"

    def __init__(self, oauth: tuple[str, str, str] | None, client: httpx.Client | None = None):
        if not oauth:
            raise MissingKey(
                "Set YOUTUBE_OAUTH_CLIENT_ID, YOUTUBE_OAUTH_CLIENT_SECRET and "
                "YOUTUBE_OAUTH_REFRESH_TOKEN for YouTube retention stats."
            )
        self.oauth, self.http = oauth, _client(client)
        self._token: str | None = None

    def _auth(self) -> dict:
        if self._token is None:
            cid, secret, refresh = self.oauth
            r = self.http.post(
                self.TOKEN_URL,
                data={
                    "client_id": cid,
                    "client_secret": secret,
                    "refresh_token": refresh,
                    "grant_type": "refresh_token",
                },
            )
            r.raise_for_status()
            self._token = r.json()["access_token"]
        return {"Authorization": f"Bearer {self._token}"}

    def stats(self, ids: list[str], since: datetime) -> dict[str, dict]:
        out: dict[str, dict] = {}
        today = datetime.now(UTC).date().isoformat()
        for i in range(0, len(ids), 200):
            r = self.http.get(
                self.REPORTS,
                headers=self._auth(),
                params={
                    "ids": "channel==MINE",
                    "startDate": since.date().isoformat(),
                    "endDate": today,
                    "metrics": self.METRICS,
                    "dimensions": "video",
                    "filters": "video==" + ",".join(ids[i : i + 200]),
                    "sort": "-views",
                    "maxResults": 200,
                },
            )
            r.raise_for_status()
            body = r.json()
            names = [h["name"] for h in body.get("columnHeaders", [])]
            for row in body.get("rows", []) or []:
                rec = dict(zip(names, row, strict=False))
                vid = str(rec.pop("video", ""))
                if vid:
                    out[vid] = {
                        "views": _int(rec.get("views")) or 0,
                        "likes": _int(rec.get("likes")),
                        "comments": _int(rec.get("comments")),
                        "shares": _int(rec.get("shares")),
                        "avg_view_seconds": _float(rec.get("averageViewDuration")),
                        "avg_view_pct": _float(rec.get("averageViewPercentage")),
                    }
        return out


class YouTubeUploader:
    """YouTube Data API resumable upload (scope ``youtube.upload``). Only called from
    :func:`redblue.video.upload.upload`, which requires a person's confirmation."""

    INIT_URL = "https://www.googleapis.com/upload/youtube/v3/videos"

    def __init__(self, oauth: tuple[str, str, str] | None, client: httpx.Client | None = None):
        if not oauth:
            raise MissingKey(
                "Set YOUTUBE_OAUTH_CLIENT_ID, YOUTUBE_OAUTH_CLIENT_SECRET and "
                "YOUTUBE_UPLOAD_REFRESH_TOKEN to upload to YouTube."
            )
        self.token = YouTubeAnalytics(oauth, client)  # same Google OAuth refresh flow
        self.http = self.token.http

    def upload(self, path, resource: dict) -> dict:
        size = path.stat().st_size
        r = self.http.post(
            self.INIT_URL,
            params={"uploadType": "resumable", "part": "snippet,status"},
            headers={
                **self.token._auth(),
                "X-Upload-Content-Length": str(size),
                "X-Upload-Content-Type": "video/mp4",
            },
            json=resource,
        )
        r.raise_for_status()
        session = r.headers.get("location", "")
        parts = urlsplit(session)
        if parts.scheme != "https" or not (
            parts.hostname == "www.googleapis.com"
            or (parts.hostname or "").endswith(".googleapis.com")
        ):
            raise ValueError("YouTube returned an unexpected upload URL.")
        r = self.http.put(
            session,
            headers={**self.token._auth(), "Content-Type": "video/mp4"},
            content=path.read_bytes(),
            timeout=httpx.Timeout(600.0, connect=10.0),
        )
        r.raise_for_status()
        body = r.json()
        if not isinstance(body.get("id"), str):
            raise ValueError("YouTube didn't return a video id.")
        return body


class PlatformError(RuntimeError):
    """A posting API answered with an error (message is safe to show to the person)."""


def _https_host(url: str, suffixes: tuple[str, ...]) -> str:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or not any(host == d or host.endswith("." + d) for d in suffixes):
        raise ValueError("The platform returned an unexpected upload URL.")
    return url


class TikTok:
    """TikTok Content Posting API. ``inbox`` uploads land in the creator's TikTok drafts
    (scope ``video.upload``); ``direct`` posts (scope ``video.publish``). Only called from
    :mod:`redblue.video.upload_social`, which requires a person's confirmation."""

    TOKEN_URL = "https://open.tiktokapis.com/v2/oauth/token/"  # noqa: S105  # nosec B105
    API = "https://open.tiktokapis.com/v2/post/publish"
    MIN_CHUNK, MAX_SINGLE, CHUNK = 5 * 2**20, 64 * 2**20, 10 * 2**20

    def __init__(
        self,
        credentials: tuple[str, str, str] | None,
        client: httpx.Client | None = None,
        on_rotate=None,
    ):
        if not credentials:
            raise MissingKey(
                "Set TIKTOK_CLIENT_KEY, TIKTOK_CLIENT_SECRET and TIKTOK_REFRESH_TOKEN to upload "
                "to TikTok."
            )
        self.credentials, self.http, self.on_rotate = credentials, _client(client), on_rotate
        self._token: str | None = None

    def _auth(self) -> dict:
        if self._token is None:
            key, secret, refresh = self.credentials
            r = self.http.post(
                self.TOKEN_URL,
                data={
                    "client_key": key,
                    "client_secret": secret,
                    "grant_type": "refresh_token",
                    "refresh_token": refresh,
                },
            )
            r.raise_for_status()
            body = r.json()
            if "access_token" not in body:
                raise PlatformError(f"TikTok login failed: {body.get('error', 'no token')}")
            self._token = body["access_token"]
            new = body.get("refresh_token")
            if new and new != refresh and self.on_rotate:
                self.on_rotate(new)
        return {"Authorization": f"Bearer {self._token}"}

    def _post(self, path: str, payload: dict) -> dict:
        r = self.http.post(
            f"{self.API}/{path}",
            headers={**self._auth(), "Content-Type": "application/json; charset=UTF-8"},
            json=payload,
        )
        body = r.json() if r.content else {}
        err = body.get("error") or {}
        if r.status_code >= 400 or err.get("code") not in (None, "ok"):
            raise PlatformError(f"TikTok: {err.get('message') or err.get('code') or r.status_code}")
        return body.get("data") or {}

    def creator_info(self) -> dict:
        return self._post("creator_info/query/", {})

    @classmethod
    def chunks(cls, size: int) -> tuple[int, int]:
        """(chunk_size, total_chunk_count) per TikTok's rules: one chunk up to 64 MB, else
        10 MB chunks with the remainder folded into the last one."""
        if size <= cls.MAX_SINGLE:
            return size, 1
        return cls.CHUNK, size // cls.CHUNK

    def upload(self, path, post_info: dict | None = None) -> str:
        """Init (inbox when ``post_info`` is None, else direct post) and send the file.
        Returns the publish_id."""
        size = path.stat().st_size
        chunk, count = self.chunks(size)
        source = {
            "source": "FILE_UPLOAD",
            "video_size": size,
            "chunk_size": chunk,
            "total_chunk_count": count,
        }
        if post_info is None:
            data = self._post("inbox/video/init/", {"source_info": source})
        else:
            data = self._post("video/init/", {"post_info": post_info, "source_info": source})
        publish_id, url = data.get("publish_id"), data.get("upload_url", "")
        if not publish_id:
            raise PlatformError("TikTok didn't return a publish id.")
        _https_host(url, ("tiktokapis.com",))
        with path.open("rb") as f:
            for i in range(count):
                start = i * chunk
                end = size - 1 if i == count - 1 else start + chunk - 1
                f.seek(start)
                r = self.http.put(
                    url,
                    headers={
                        "Content-Type": "video/mp4",
                        "Content-Range": f"bytes {start}-{end}/{size}",
                    },
                    content=f.read(end - start + 1),
                    timeout=httpx.Timeout(600.0, connect=10.0),
                )
                r.raise_for_status()
        return publish_id

    def status(self, publish_id: str) -> dict:
        return self._post("status/fetch/", {"publish_id": publish_id})


class Instagram:
    """Instagram Graph API Reels publishing: a resumable-upload container first (not
    public), then ``media_publish`` (public), which only a person's click triggers."""

    RUPLOAD = "https://rupload.facebook.com/ig-api-upload"

    def __init__(
        self,
        credentials: tuple[str, str] | None,
        host: str = "graph.facebook.com",
        version: str = "v25.0",
        client: httpx.Client | None = None,
    ):
        if not credentials:
            raise MissingKey("Set INSTAGRAM_ACCESS_TOKEN and INSTAGRAM_USER_ID to post Reels.")
        if host not in ("graph.facebook.com", "graph.instagram.com"):
            raise ValueError("Unsupported Instagram API host.")
        if not re.fullmatch(r"v\d{1,3}\.\d", version):
            raise ValueError("Instagram API version looks like v25.0.")
        self.token, self.user_id = credentials
        if not re.fullmatch(r"\d{1,30}", self.user_id):
            raise ValueError("INSTAGRAM_USER_ID must be the numeric account id.")
        self.base, self.version, self.http = f"https://{host}/{version}", version, _client(client)

    @property
    def _auth(self) -> dict:
        return {"Authorization": f"Bearer {self.token}"}

    def _json(self, r: httpx.Response) -> dict:
        body = r.json() if r.content else {}
        if r.status_code >= 400 or "error" in body:
            err = body.get("error") or {}
            raise PlatformError(f"Instagram: {err.get('message') or r.status_code}")
        return body

    def create_reel(self, caption: str, share_to_feed: bool = True) -> str:
        r = self.http.post(
            f"{self.base}/{self.user_id}/media",
            headers=self._auth,
            data={
                "media_type": "REELS",
                "upload_type": "resumable",
                "caption": caption,
                "share_to_feed": "true" if share_to_feed else "false",
            },
        )
        cid = str(self._json(r).get("id", ""))
        if not re.fullmatch(r"\d{1,40}", cid):
            raise PlatformError("Instagram didn't return a container id.")
        return cid

    def send_file(self, container_id: str, path) -> None:
        data = path.read_bytes()
        r = self.http.post(
            f"{self.RUPLOAD}/{self.version}/{container_id}",
            headers={
                "Authorization": f"OAuth {self.token}",
                "offset": "0",
                "file_size": str(len(data)),
            },
            content=data,
            timeout=httpx.Timeout(600.0, connect=10.0),
        )
        self._json(r)

    def status(self, container_id: str) -> dict:
        r = self.http.get(
            f"{self.base}/{container_id}",
            headers=self._auth,
            params={"fields": "status_code,status"},
        )
        return self._json(r)

    def publish(self, container_id: str) -> str:
        r = self.http.post(
            f"{self.base}/{self.user_id}/media_publish",
            headers=self._auth,
            data={"creation_id": container_id},
        )
        mid = str(self._json(r).get("id", ""))
        if not mid:
            raise PlatformError("Instagram didn't return a media id.")
        return mid

    def permalink(self, media_id: str) -> str | None:
        r = self.http.get(
            f"{self.base}/{media_id}", headers=self._auth, params={"fields": "permalink"}
        )
        return self._json(r).get("permalink")


class FacebookPage:
    """Facebook Page Reels (Graph API ``video_reels``): start, transfer, finish. Finishing
    as ``DRAFT`` keeps it off the Page until someone publishes it in Meta Business Suite."""

    def __init__(
        self,
        credentials: tuple[str, str] | None,
        version: str = "v25.0",
        client: httpx.Client | None = None,
    ):
        if not credentials:
            raise MissingKey("Set FACEBOOK_PAGE_ACCESS_TOKEN and FACEBOOK_PAGE_ID to post Reels.")
        self.token, self.page_id = credentials
        if not re.fullmatch(r"\d{1,30}", self.page_id):
            raise ValueError("FACEBOOK_PAGE_ID must be the numeric Page id.")
        if not re.fullmatch(r"v\d{1,3}\.\d", version):
            raise ValueError("Facebook API version looks like v25.0.")
        self.base, self.http = f"https://graph.facebook.com/{version}", _client(client)

    @property
    def _auth(self) -> dict:
        return {"Authorization": f"Bearer {self.token}"}

    def _json(self, r: httpx.Response) -> dict:
        body = r.json() if r.content else {}
        if r.status_code >= 400 or "error" in body:
            err = body.get("error") or {}
            raise PlatformError(f"Facebook: {err.get('message') or r.status_code}")
        return body

    def upload(self, path, *, description: str, title: str, state: str) -> str:
        if state not in ("DRAFT", "PUBLISHED"):
            raise ValueError("Unsupported Facebook video state.")
        r = self.http.post(
            f"{self.base}/{self.page_id}/video_reels",
            headers=self._auth,
            data={"upload_phase": "start"},
        )
        body = self._json(r)
        video_id, url = str(body.get("video_id", "")), body.get("upload_url", "")
        if not re.fullmatch(r"\d{1,40}", video_id):
            raise PlatformError("Facebook didn't return a video id.")
        _https_host(url, ("rupload.facebook.com",))
        data = path.read_bytes()
        r = self.http.post(
            url,
            headers={
                "Authorization": f"OAuth {self.token}",
                "offset": "0",
                "file_size": str(len(data)),
            },
            content=data,
            timeout=httpx.Timeout(600.0, connect=10.0),
        )
        self._json(r)
        r = self.http.post(
            f"{self.base}/{self.page_id}/video_reels",
            headers=self._auth,
            data={
                "upload_phase": "finish",
                "video_id": video_id,
                "video_state": state,
                "description": description,
                "title": title,
            },
        )
        if not self._json(r).get("success", True):
            raise PlatformError("Facebook didn't accept the Reel.")
        return video_id

    def status(self, video_id: str) -> dict:
        r = self.http.get(
            f"{self.base}/{video_id}", headers=self._auth, params={"fields": "status"}
        )
        return self._json(r).get("status") or {}


def little_text(text: str) -> str:
    """Escape LinkedIn's "little text" reserved characters so commentary is shown as typed."""
    return re.sub(r"([\\|{}@\[\]()<>#*_~])", r"\\\1", text)


class LinkedIn:
    """LinkedIn Videos API upload, then (separately, on a person's click) a Posts API post."""

    API = "https://api.linkedin.com/rest"
    _URN = re.compile(r"urn:li:(person|organization):[A-Za-z0-9_-]{1,64}")

    def __init__(
        self,
        credentials: tuple[str, str] | None,
        version: str = "202606",
        client: httpx.Client | None = None,
    ):
        if not credentials:
            raise MissingKey("Set LINKEDIN_ACCESS_TOKEN and LINKEDIN_AUTHOR_URN to post videos.")
        self.token, self.author = credentials
        if not self._URN.fullmatch(self.author):
            raise ValueError(
                "LINKEDIN_AUTHOR_URN looks like urn:li:person:… or urn:li:organization:…"
            )
        if not re.fullmatch(r"\d{6}", version):
            raise ValueError("LinkedIn API version looks like 202606 (YYYYMM).")
        self.version, self.http = version, _client(client)

    @property
    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.token}",
            "LinkedIn-Version": self.version,
            "X-Restli-Protocol-Version": "2.0.0",
        }

    def _json(self, r: httpx.Response) -> dict:
        body = r.json() if r.content else {}
        if r.status_code >= 400:
            raise PlatformError(f"LinkedIn: {body.get('message') or r.status_code}")
        return body

    def upload(self, path) -> str:
        data = path.read_bytes()
        r = self.http.post(
            f"{self.API}/videos",
            params={"action": "initializeUpload"},
            headers=self._headers,
            json={
                "initializeUploadRequest": {
                    "owner": self.author,
                    "fileSizeBytes": len(data),
                    "uploadCaptions": False,
                    "uploadThumbnail": False,
                }
            },
        )
        value = self._json(r).get("value") or {}
        video, token = value.get("video", ""), value.get("uploadToken", "")
        if not str(video).startswith("urn:li:video:"):
            raise PlatformError("LinkedIn didn't return a video id.")
        etags = []
        for part in sorted(value.get("uploadInstructions", []), key=lambda p: p["firstByte"]):
            url = _https_host(part["uploadUrl"], ("linkedin.com", "licdn.com"))
            first, last = int(part["firstByte"]), int(part["lastByte"])
            r = self.http.put(
                url,
                headers={"Content-Type": "application/octet-stream"},
                content=data[first : last + 1],
                timeout=httpx.Timeout(600.0, connect=10.0),
            )
            r.raise_for_status()
            etags.append(r.headers.get("etag", ""))
        if not etags:
            raise PlatformError("LinkedIn didn't return upload instructions.")
        r = self.http.post(
            f"{self.API}/videos",
            params={"action": "finalizeUpload"},
            headers=self._headers,
            json={
                "finalizeUploadRequest": {
                    "video": video,
                    "uploadToken": token,
                    "uploadedPartIds": etags,
                }
            },
        )
        self._json(r)
        return video

    def video_status(self, video: str) -> dict:
        r = self.http.get(f"{self.API}/videos/{quote(video, safe='')}", headers=self._headers)
        return self._json(r)

    def post(self, *, video: str, commentary: str, title: str, visibility: str) -> str:
        if visibility not in ("PUBLIC", "CONNECTIONS"):
            raise ValueError("LinkedIn visibility must be PUBLIC or CONNECTIONS.")
        r = self.http.post(
            f"{self.API}/posts",
            headers={**self._headers, "Content-Type": "application/json"},
            json={
                "author": self.author,
                "commentary": little_text(commentary),
                "visibility": visibility,
                "distribution": {
                    "feedDistribution": "MAIN_FEED",
                    "targetEntities": [],
                    "thirdPartyDistributionChannels": [],
                },
                "content": {"media": {"title": title[:200], "id": video}},
                "lifecycleState": "PUBLISHED",
                "isReshareDisabledByAuthor": False,
            },
        )
        self._json(r)
        urn = r.headers.get("x-restli-id", "")
        if not re.fullmatch(r"urn:li:(share|ugcPost):\d{1,40}", urn):
            raise PlatformError("LinkedIn didn't return the post id.")
        return urn


class XClient:
    """X API v2: chunked media upload, then (separately, on a person's click) a post.
    OAuth 2.0 user context; X refresh tokens are single-use, so every refresh hands the new
    one to ``on_rotate`` straight away."""

    TOKEN_URL = "https://api.x.com/2/oauth2/token"  # noqa: S105  # nosec B105
    API = "https://api.x.com/2"
    CHUNK = 4 * 2**20  # X accepts segments under 5 MB

    def __init__(
        self,
        credentials: tuple[str, str, str] | None,
        client: httpx.Client | None = None,
        on_rotate=None,
    ):
        if not credentials:
            raise MissingKey(
                "Set X_CLIENT_ID, X_REFRESH_TOKEN and RB_VIDEO_X_TOKEN_FILE to post on X."
            )
        self.credentials, self.http, self.on_rotate = credentials, _client(client), on_rotate
        self._token: str | None = None

    def _auth(self) -> dict:
        if self._token is None:
            client_id, secret, refresh = self.credentials
            r = self.http.post(
                self.TOKEN_URL,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh,
                    "client_id": client_id,
                },
                auth=(client_id, secret) if secret else None,
            )
            body = r.json() if r.content else {}
            if r.status_code >= 400 or "access_token" not in body:
                raise PlatformError(
                    f"X login failed: {body.get('error_description') or r.status_code}"
                )
            self._token = body["access_token"]
            if body.get("refresh_token") and self.on_rotate:
                self.on_rotate(body["refresh_token"])  # the old one no longer works
        return {"Authorization": f"Bearer {self._token}"}

    def _json(self, r: httpx.Response) -> dict:
        body = r.json() if r.content else {}
        if r.status_code >= 400 or (body.get("errors") and not body.get("data")):
            errors = body.get("errors") or [{}]
            detail = body.get("detail") or errors[0].get("message") or r.status_code
            raise PlatformError(f"X: {detail}")
        return body

    def upload(self, path) -> tuple[str, str]:
        """Returns (media_id, processing state)."""
        data = path.read_bytes()
        r = self.http.post(
            f"{self.API}/media/upload/initialize",
            headers=self._auth(),
            json={
                "media_type": "video/mp4",
                "total_bytes": len(data),
                "media_category": "tweet_video",
            },
        )
        media_id = str((self._json(r).get("data") or {}).get("id", ""))
        if not re.fullmatch(r"\d{1,30}", media_id):
            raise PlatformError("X didn't return a media id.")
        for i in range(0, max(1, -(-len(data) // self.CHUNK))):
            r = self.http.post(
                f"{self.API}/media/upload/{media_id}/append",
                headers=self._auth(),
                data={"segment_index": str(i)},
                files={"media": ("blob", data[i * self.CHUNK : (i + 1) * self.CHUNK])},
                timeout=httpx.Timeout(600.0, connect=10.0),
            )
            self._json(r)
        r = self.http.post(f"{self.API}/media/upload/{media_id}/finalize", headers=self._auth())
        info = (self._json(r).get("data") or {}).get("processing_info") or {}
        return media_id, info.get("state", "succeeded")

    def media_state(self, media_id: str) -> tuple[str, str]:
        """(state, error message) of an uploaded video's processing."""
        r = self.http.get(
            f"{self.API}/media/upload",
            headers=self._auth(),
            params={"media_id": media_id, "command": "STATUS"},
        )
        info = (self._json(r).get("data") or {}).get("processing_info") or {}
        error = (info.get("error") or {}).get("message", "")
        return info.get("state", "succeeded"), error

    def post(self, text: str, media_id: str) -> str:
        r = self.http.post(
            f"{self.API}/tweets",
            headers=self._auth(),
            json={"text": text, "media": {"media_ids": [media_id]}},
        )
        post_id = str((self._json(r).get("data") or {}).get("id", ""))
        if not re.fullmatch(r"\d{1,30}", post_id):
            raise PlatformError("X didn't return the post id.")
        return post_id


class Threads:
    """Threads API video posts: a container that Meta fills by fetching ``video_url`` (a
    short-lived signed link to the render), then ``threads_publish`` on a person's click."""

    BASE = "https://graph.threads.net/v1.0"

    def __init__(self, credentials: tuple[str, str] | None, client: httpx.Client | None = None):
        if not credentials:
            raise MissingKey("Set THREADS_ACCESS_TOKEN and THREADS_USER_ID to post on Threads.")
        self.token, self.user_id = credentials
        if not re.fullmatch(r"\d{1,30}", self.user_id):
            raise ValueError("THREADS_USER_ID must be the numeric Threads user id.")
        self.http = _client(client)

    @property
    def _auth(self) -> dict:
        return {"Authorization": f"Bearer {self.token}"}

    def _json(self, r: httpx.Response) -> dict:
        body = r.json() if r.content else {}
        if r.status_code >= 400 or "error" in body:
            err = body.get("error") or {}
            raise PlatformError(f"Threads: {err.get('message') or r.status_code}")
        return body

    def create_video(self, video_url: str, text: str) -> str:
        r = self.http.post(
            f"{self.BASE}/{self.user_id}/threads",
            headers=self._auth,
            data={"media_type": "VIDEO", "video_url": video_url, "text": text},
        )
        cid = str(self._json(r).get("id", ""))
        if not re.fullmatch(r"\d{1,40}", cid):
            raise PlatformError("Threads didn't return a container id.")
        return cid

    def status(self, container_id: str) -> dict:
        r = self.http.get(
            f"{self.BASE}/{container_id}",
            headers=self._auth,
            params={"fields": "status,error_message"},
        )
        return self._json(r)

    def publish(self, container_id: str) -> str:
        r = self.http.post(
            f"{self.BASE}/{self.user_id}/threads_publish",
            headers=self._auth,
            data={"creation_id": container_id},
        )
        mid = str(self._json(r).get("id", ""))
        if not mid:
            raise PlatformError("Threads didn't return a post id.")
        return mid

    def permalink(self, media_id: str) -> str | None:
        r = self.http.get(
            f"{self.BASE}/{media_id}", headers=self._auth, params={"fields": "permalink"}
        )
        return self._json(r).get("permalink")


class Pinterest:
    """Pinterest API v5 video Pins: register + upload the video (not visible), then a Pin on
    a board, created only on a person's click. Continuous refresh tokens are handed to
    ``on_rotate`` whenever Pinterest issues a new one."""

    TOKEN_PATH = "/v5/oauth/token"  # noqa: S105  # nosec B105

    def __init__(
        self,
        credentials: tuple[str, str, str] | None,
        board_id: str | None,
        host: str = "api.pinterest.com",
        client: httpx.Client | None = None,
        on_rotate=None,
    ):
        if not credentials or not board_id:
            raise MissingKey(
                "Set PINTEREST_APP_ID, PINTEREST_APP_SECRET, PINTEREST_REFRESH_TOKEN and "
                "RB_VIDEO_PINTEREST_BOARD_ID to post Pins."
            )
        if host not in ("api.pinterest.com", "api-sandbox.pinterest.com"):
            raise ValueError("Unsupported Pinterest API host.")
        if not re.fullmatch(r"\d{1,30}", board_id):
            raise ValueError("RB_VIDEO_PINTEREST_BOARD_ID must be the numeric board id.")
        self.credentials, self.board_id = credentials, board_id
        self.base, self.http, self.on_rotate = f"https://{host}", _client(client), on_rotate
        self._token: str | None = None

    def _auth(self) -> dict:
        if self._token is None:
            app_id, secret, refresh = self.credentials
            r = self.http.post(
                self.base + self.TOKEN_PATH,
                auth=(app_id, secret),
                data={"grant_type": "refresh_token", "refresh_token": refresh},
            )
            body = r.json() if r.content else {}
            if r.status_code >= 400 or "access_token" not in body:
                raise PlatformError(
                    f"Pinterest login failed: {body.get('message') or r.status_code}"
                )
            self._token = body["access_token"]
            new = body.get("refresh_token")
            if new and new != refresh and self.on_rotate:
                self.on_rotate(new)
        return {"Authorization": f"Bearer {self._token}"}

    def _json(self, r: httpx.Response) -> dict:
        body = r.json() if r.content else {}
        if r.status_code >= 400:
            raise PlatformError(f"Pinterest: {body.get('message') or r.status_code}")
        return body

    def upload(self, path) -> str:
        r = self.http.post(
            f"{self.base}/v5/media", headers=self._auth(), json={"media_type": "video"}
        )
        body = self._json(r)
        media_id, url = str(body.get("media_id", "")), body.get("upload_url", "")
        if not re.fullmatch(r"\d{1,40}", media_id):
            raise PlatformError("Pinterest didn't return a media id.")
        _https_host(url, ("amazonaws.com", "pinterest.com", "pinimg.com"))
        fields = {str(k): str(v) for k, v in (body.get("upload_parameters") or {}).items()}
        r = self.http.post(  # pre-signed storage upload: its own fields, no Pinterest token
            url,
            data=fields,
            files={"file": (path.name, path.read_bytes(), "video/mp4")},
            timeout=httpx.Timeout(600.0, connect=10.0),
        )
        if r.status_code >= 400:
            raise PlatformError(f"Pinterest video upload failed ({r.status_code}).")
        return media_id

    def media_status(self, media_id: str) -> str:
        r = self.http.get(f"{self.base}/v5/media/{media_id}", headers=self._auth())
        return self._json(r).get("status", "")

    def create_pin(
        self, *, media_id: str, title: str, description: str, link: str, alt_text: str
    ) -> str:
        pin = {
            "board_id": self.board_id,
            "title": title,
            "description": description,
            "alt_text": alt_text,
            "media_source": {
                "source_type": "video_id",
                "media_id": media_id,
                "cover_image_key_frame_time": 1,
            },
        }
        if link:
            pin["link"] = link
        r = self.http.post(f"{self.base}/v5/pins", headers=self._auth(), json=pin)
        pin_id = str(self._json(r).get("id", ""))
        if not re.fullmatch(r"\d{1,40}", pin_id):
            raise PlatformError("Pinterest didn't return the Pin id.")
        return pin_id


class RedditPoster:
    """Reddit video posts with a user's refresh token (scopes ``submit``, ``identity``,
    ``read``). Video posts go through Reddit's media-asset upload (the flow PRAW uses), then
    ``/api/submit`` on a person's click. Reddit answers video submissions asynchronously, so
    the post's link is found afterwards in the account's submitted list."""

    TOKEN_URL = "https://www.reddit.com/api/v1/access_token"  # noqa: S105  # nosec B105
    API = "https://oauth.reddit.com"
    SUBREDDIT = re.compile(r"[A-Za-z0-9_]{2,21}")

    def __init__(
        self,
        credentials: tuple[str, str] | None,
        refresh_token: str | None,
        username: str | None,
        user_agent: str,
        client: httpx.Client | None = None,
    ):
        if not (credentials and refresh_token and username):
            raise MissingKey(
                "Set REDDIT_CLIENT_ID, REDDIT_CLIENT_SECRET, REDDIT_POST_REFRESH_TOKEN and "
                "REDDIT_USERNAME to post on Reddit."
            )
        if not re.fullmatch(r"[A-Za-z0-9_-]{3,20}", username):
            raise ValueError("REDDIT_USERNAME looks like a Reddit username, without u/.")
        self.credentials, self.refresh_token, self.username = credentials, refresh_token, username
        self.http, self.headers = _client(client), {"User-Agent": user_agent}
        self._token: str | None = None

    def _auth(self) -> dict:
        if self._token is None:
            r = self.http.post(
                self.TOKEN_URL,
                auth=self.credentials,
                headers=self.headers,
                data={"grant_type": "refresh_token", "refresh_token": self.refresh_token},
            )
            body = r.json() if r.content else {}
            if r.status_code >= 400 or "access_token" not in body:
                raise PlatformError(f"Reddit login failed: {body.get('error') or r.status_code}")
            self._token = body["access_token"]
        return {**self.headers, "Authorization": f"Bearer {self._token}"}

    def upload_asset(self, path, mimetype: str) -> str:
        r = self.http.post(
            f"{self.API}/api/media/asset.json",
            headers=self._auth(),
            data={"filepath": path.name, "mimetype": mimetype},
        )
        if r.status_code >= 400:
            raise PlatformError(f"Reddit: media upload refused ({r.status_code}).")
        lease = (r.json() or {}).get("args") or {}
        action = str(lease.get("action", ""))
        url = _https_host(
            "https:" + action if action.startswith("//") else action,
            ("amazonaws.com", "reddit.com", "redd.it", "redditmedia.com"),
        )
        fields = {str(f["name"]): str(f["value"]) for f in lease.get("fields", [])}
        if "key" not in fields:
            raise PlatformError("Reddit didn't return an upload key.")
        r = self.http.post(  # pre-signed storage upload: its own fields, no Reddit token
            url,
            headers=self.headers,
            data=fields,
            files={"file": (path.name, path.read_bytes(), mimetype)},
            timeout=httpx.Timeout(600.0, connect=10.0),
        )
        if r.status_code >= 400:
            raise PlatformError(f"Reddit media upload failed ({r.status_code}).")
        return f"{url.rstrip('/')}/{fields['key']}"

    def submit_video(
        self, *, subreddit: str, title: str, video_url: str, poster_url: str, nsfw: bool
    ) -> None:
        if not self.SUBREDDIT.fullmatch(subreddit):
            raise ValueError("Enter a subreddit name like technology (without r/).")
        r = self.http.post(
            f"{self.API}/api/submit",
            headers=self._auth(),
            data={
                "sr": subreddit,
                "title": title,
                "kind": "video",
                "url": video_url,
                "video_poster_url": poster_url,
                "api_type": "json",
                "nsfw": "true" if nsfw else "false",
                "spoiler": "false",
                "sendreplies": "true",
                "resubmit": "true",
            },
        )
        body = (r.json() if r.content else {}).get("json") or {}
        errors = body.get("errors") or []
        if r.status_code >= 400 or errors:
            detail = "; ".join(" ".join(str(x) for x in e[:2]) for e in errors) or r.status_code
            raise PlatformError(f"Reddit: {detail}")

    def find_post(self, subreddit: str, title: str, since: float) -> str | None:
        r = self.http.get(
            f"{self.API}/user/{self.username}/submitted",
            headers=self._auth(),
            params={"limit": 25, "sort": "new", "raw_json": 1},
        )
        if r.status_code >= 400:
            raise PlatformError(f"Reddit: couldn't read your posts ({r.status_code}).")
        for child in (r.json().get("data") or {}).get("children", []):
            d = child.get("data") or {}
            if (
                str(d.get("subreddit", "")).lower() == subreddit.lower()
                and d.get("title") == title
                and float(d.get("created_utc") or 0) >= since - 120
                and str(d.get("permalink", "")).startswith("/r/")
            ):
                return "https://www.reddit.com" + d["permalink"]
        return None


class Bluesky:
    """Bluesky (AT Protocol): log in with an app password, upload the video to Bluesky's
    video service, then (on a person's click) create a post embedding the processed blob."""

    VIDEO = "https://video.bsky.app"

    def __init__(
        self,
        credentials: tuple[str, str] | None,
        pds: str = "https://bsky.social",
        client: httpx.Client | None = None,
    ):
        if not credentials:
            raise MissingKey("Set BLUESKY_HANDLE and BLUESKY_APP_PASSWORD to post on Bluesky.")
        self.handle, self.password = credentials
        self.pds = _https_host(pds.rstrip("/"), (urlsplit(pds).hostname or "x",))
        self.http = _client(client)
        self._session: dict | None = None

    def _json(self, r: httpx.Response) -> dict:
        body = r.json() if r.content else {}
        if r.status_code >= 400:
            raise PlatformError(
                f"Bluesky: {body.get('message') or body.get('error') or r.status_code}"
            )
        return body

    def session(self) -> dict:
        if self._session is None:
            r = self.http.post(
                f"{self.pds}/xrpc/com.atproto.server.createSession",
                json={"identifier": self.handle, "password": self.password},
            )
            self._session = self._json(r)
        return self._session

    def _pds_host(self) -> str:
        """The account's own PDS (bsky.social accounts live on a host.bsky.network PDS)."""
        for svc in (self.session().get("didDoc") or {}).get("service", []):
            if svc.get("id", "").endswith("#atproto_pds"):
                return urlsplit(svc.get("serviceEndpoint", "")).hostname or ""
        return urlsplit(self.pds).hostname or ""

    def upload_video(self, path) -> str:
        """Returns the processing job id."""
        sess = self.session()
        r = self.http.get(
            f"{self.pds}/xrpc/com.atproto.server.getServiceAuth",
            headers={"Authorization": f"Bearer {sess['accessJwt']}"},
            params={
                "aud": f"did:web:{self._pds_host()}",
                "lxm": "com.atproto.repo.uploadBlob",
                "exp": int(datetime.now(UTC).timestamp()) + 1800,
            },
        )
        token = self._json(r)["token"]
        r = self.http.post(
            f"{self.VIDEO}/xrpc/app.bsky.video.uploadVideo",
            params={"did": sess["did"], "name": path.name},
            headers={"Authorization": f"Bearer {token}", "Content-Type": "video/mp4"},
            content=path.read_bytes(),
            timeout=httpx.Timeout(600.0, connect=10.0),
        )
        body = r.json() if r.content else {}
        job = body.get("jobStatus") or body  # 409 "already_exists" still carries the job
        if r.status_code >= 400 and not job.get("jobId"):
            raise PlatformError(f"Bluesky: {body.get('message') or r.status_code}")
        if not job.get("jobId"):
            raise PlatformError("Bluesky didn't return a video job.")
        return str(job["jobId"])

    def job_status(self, job_id: str) -> dict:
        r = self.http.get(
            f"{self.VIDEO}/xrpc/app.bsky.video.getJobStatus", params={"jobId": job_id}
        )
        return self._json(r).get("jobStatus") or {}

    def post(self, *, text: str, blob: dict, width: int, height: int) -> str:
        sess = self.session()
        record = {
            "$type": "app.bsky.feed.post",
            "text": text,
            "createdAt": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "embed": {
                "$type": "app.bsky.embed.video",
                "video": blob,
                "aspectRatio": {"width": width, "height": height},
            },
        }
        facets = hashtag_facets(text)
        if facets:
            record["facets"] = facets
        r = self.http.post(
            f"{self.pds}/xrpc/com.atproto.repo.createRecord",
            headers={"Authorization": f"Bearer {sess['accessJwt']}"},
            json={"repo": sess["did"], "collection": "app.bsky.feed.post", "record": record},
        )
        uri = str(self._json(r).get("uri", ""))
        m = re.fullmatch(
            r"at://(did:[a-z]+:[A-Za-z0-9._:%-]+)/app\.bsky\.feed\.post/([A-Za-z0-9]+)", uri
        )
        if not m:
            raise PlatformError("Bluesky didn't return the post id.")
        return m.group(2)


def hashtag_facets(text: str) -> list[dict]:
    """Rich-text facets so #tags are clickable (offsets are UTF-8 bytes)."""
    facets = []
    for m in re.finditer(r"(?:^|\s)(#([^\s#]{1,64}))", text):
        tag = m.group(2).rstrip(".,!?;:")
        if not tag or tag.isdigit():
            continue
        start = len(text[: m.start(1)].encode())
        facets.append(
            {
                "index": {"byteStart": start, "byteEnd": start + len(("#" + tag).encode())},
                "features": [{"$type": "app.bsky.richtext.facet#tag", "tag": tag}],
            }
        )
    return facets


class Tumblr:
    """Tumblr API v2 (NPF): one multipart request creates the post and uploads the video.
    The post state (draft / private / published) is chosen by the person. OAuth2 refresh
    returns a new refresh token each time, handed to ``on_rotate``."""

    API = "https://api.tumblr.com/v2"
    USER_AGENT = "redblue-video/0.1 (self-hosted)"

    def __init__(
        self,
        credentials: tuple[str, str, str] | None,
        blog: str | None,
        client: httpx.Client | None = None,
        on_rotate=None,
    ):
        if not credentials or not blog:
            raise MissingKey(
                "Set TUMBLR_CLIENT_ID, TUMBLR_CLIENT_SECRET, TUMBLR_REFRESH_TOKEN and "
                "RB_VIDEO_TUMBLR_BLOG to post on Tumblr."
            )
        if not re.fullmatch(r"(t:[A-Za-z0-9_-]{8,40}|[A-Za-z0-9][A-Za-z0-9.-]{0,99})", blog):
            raise ValueError("RB_VIDEO_TUMBLR_BLOG looks like a blog name (e.g. myblog).")
        self.credentials, self.blog = credentials, blog
        self.http, self.on_rotate = _client(client), on_rotate
        self._token: str | None = None

    def _auth(self) -> dict:
        if self._token is None:
            client_id, secret, refresh = self.credentials
            r = self.http.post(
                f"{self.API}/oauth2/token",
                headers={"User-Agent": self.USER_AGENT},
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh,
                    "client_id": client_id,
                    "client_secret": secret,
                },
            )
            body = r.json() if r.content else {}
            if r.status_code >= 400 or "access_token" not in body:
                raise PlatformError(
                    f"Tumblr login failed: {body.get('error_description') or r.status_code}"
                )
            self._token = body["access_token"]
            new = body.get("refresh_token")
            if new and new != refresh and self.on_rotate:
                self.on_rotate(new)
        return {"Authorization": f"Bearer {self._token}", "User-Agent": self.USER_AGENT}

    def _json(self, r: httpx.Response) -> dict:
        body = r.json() if r.content else {}
        if r.status_code >= 400:
            errors = body.get("errors") or [{}]
            detail = errors[0].get("detail") or (body.get("meta") or {}).get("msg")
            raise PlatformError(f"Tumblr: {detail or r.status_code}")
        return body.get("response") or {}

    def create_video_post(
        self, path, *, caption: str, tags: list[str], state: str, width: int, height: int
    ) -> str:
        if state not in ("draft", "private", "published"):
            raise ValueError("Unsupported Tumblr post state.")
        content = []
        if caption:
            content.append({"type": "text", "text": caption})
        content.append(
            {
                "type": "video",
                "media": {
                    "type": "video/mp4",
                    "identifier": "video",
                    "width": width,
                    "height": height,
                },
            }
        )
        body = {"content": content, "state": state, "tags": ",".join(tags)}
        r = self.http.post(
            f"{self.API}/blog/{self.blog}/posts",
            headers=self._auth(),
            files=[
                ("json", (None, json.dumps(body), "application/json")),
                ("video", (path.name, path.read_bytes(), "video/mp4")),
            ],
            timeout=httpx.Timeout(600.0, connect=10.0),
        )
        post_id = str(self._json(r).get("id", ""))
        if not re.fullmatch(r"\d{1,30}", post_id):
            raise PlatformError("Tumblr didn't return the post id.")
        return post_id

    def post_state(self, post_id: str) -> str:
        r = self.http.get(f"{self.API}/blog/{self.blog}/posts/{post_id}", headers=self._auth())
        return str(self._json(r).get("state", ""))


class Vimeo:
    """Vimeo API: create the video with its privacy, send the file with tus (resumable
    PATCH), then read transcode/privacy status. The person picks the privacy."""

    API = "https://api.vimeo.com"
    ACCEPT = "application/vnd.vimeo.*+json;version=3.4"

    def __init__(self, token: str | None, client: httpx.Client | None = None):
        if not token:
            raise MissingKey("Set VIMEO_ACCESS_TOKEN to upload to Vimeo.")
        self.token, self.http = token, _client(client)

    @property
    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}", "Accept": self.ACCEPT}

    def _json(self, r: httpx.Response) -> dict:
        body = r.json() if r.content else {}
        if r.status_code >= 400:
            detail = body.get("developer_message") or body.get("error") or r.status_code
            raise PlatformError(f"Vimeo: {detail}")
        return body

    def upload(self, path, *, name: str, description: str, privacy: str) -> tuple[str, str]:
        """Returns (video id, link)."""
        if privacy not in ("nobody", "unlisted", "anybody"):
            raise ValueError("Unsupported Vimeo privacy.")
        data = path.read_bytes()
        r = self.http.post(
            f"{self.API}/me/videos",
            headers={**self._headers, "Content-Type": "application/json"},
            json={
                "upload": {"approach": "tus", "size": len(data)},
                "name": name,
                "description": description,
                "privacy": {"view": privacy},
            },
        )
        body = self._json(r)
        m = re.fullmatch(r"/videos/(\d{1,20})", str(body.get("uri", "")))
        if not m:
            raise PlatformError("Vimeo didn't return a video id.")
        url = _https_host((body.get("upload") or {}).get("upload_link", ""), ("vimeo.com",))
        offset = 0
        for _ in range(20):  # resume from Vimeo's reported offset if a PATCH stops short
            r = self.http.patch(
                url,
                headers={
                    "Tus-Resumable": "1.0.0",
                    "Upload-Offset": str(offset),
                    "Content-Type": "application/offset+octet-stream",
                },
                content=data[offset:],
                timeout=httpx.Timeout(600.0, connect=10.0),
            )
            if r.status_code >= 400:
                raise PlatformError(f"Vimeo upload failed ({r.status_code}).")
            offset = int(r.headers.get("upload-offset", len(data)))
            if offset >= len(data):
                break
        else:
            raise PlatformError("Vimeo didn't accept the whole file.")
        return m.group(1), str(body.get("link") or f"https://vimeo.com/{m.group(1)}")

    def status(self, video_id: str) -> dict:
        r = self.http.get(
            f"{self.API}/videos/{video_id}",
            headers=self._headers,
            params={"fields": "transcode.status,privacy.view,link"},
        )
        return self._json(r)


class Dailymotion:
    """Dailymotion Partner API with a private API key (client credentials, scope
    ``manage_videos``): get an upload URL, send the file there, then create the video on
    your channel as a draft, private (link only) or public. The person picks which."""

    TOKEN_URL = "https://partner.api.dailymotion.com/oauth/v1/token"  # noqa: S105  # nosec B105
    API = "https://partner.api.dailymotion.com/rest"
    _XID = re.compile(r"^x[0-9a-z]{2,20}$")

    def __init__(
        self,
        credentials: tuple[str, str] | None,
        channel_id: str,
        client: httpx.Client | None = None,
    ):
        if not credentials or not channel_id:
            raise MissingKey(
                "Set DAILYMOTION_API_KEY, DAILYMOTION_API_SECRET and DAILYMOTION_CHANNEL_ID "
                "to upload to Dailymotion."
            )
        if not self._XID.match(channel_id):
            raise ValueError("DAILYMOTION_CHANNEL_ID should look like x2abcd.")
        self.credentials, self.channel, self.http = credentials, channel_id, _client(client)
        self._token: str | None = None

    def _json(self, r: httpx.Response) -> dict:
        try:
            body = r.json() if r.content else {}
        except ValueError:
            body = {}
        if r.status_code >= 400 or "error" in body:
            err = body.get("error")
            detail = (err.get("message") if isinstance(err, dict) else err) or r.status_code
            raise PlatformError(f"Dailymotion: {detail}")
        return body

    def _auth(self) -> dict:
        if self._token is None:
            key, secret = self.credentials
            body = self._json(
                self.http.post(
                    self.TOKEN_URL,
                    data={
                        "grant_type": "client_credentials",
                        "client_id": key,
                        "client_secret": secret,
                        "scope": "manage_videos",
                    },
                )
            )
            if not body.get("access_token"):
                raise PlatformError("Dailymotion didn't return an access token.")
            self._token = body["access_token"]
        return {"Authorization": f"Bearer {self._token}"}

    def upload(
        self,
        path,
        *,
        title: str,
        description: str,
        tags: list[str],
        category: str,
        visibility: str,
        for_kids: bool,
    ) -> str:
        """Returns the video id. ``visibility``: draft, private (link only) or public."""
        if visibility not in ("draft", "private", "public"):
            raise ValueError("Unsupported Dailymotion visibility.")
        r = self.http.get(f"{self.API}/file/upload", headers=self._auth())
        url = _https_host(self._json(r).get("upload_url", ""), ("dailymotion.com",))
        r = self.http.post(  # pre-signed upload URL: no API token
            url,
            files={"file": (path.name, path.read_bytes(), "video/mp4")},
            timeout=httpx.Timeout(600.0, connect=10.0),
        )
        file_url = self._json(r).get("url")
        if not file_url:
            raise PlatformError("Dailymotion didn't accept the file.")
        r = self.http.post(
            f"{self.API}/user/{self.channel}/videos",
            headers=self._auth(),
            data={
                "url": file_url,
                "title": title,
                "description": description,
                "tags": ",".join(tags),
                "channel": category,
                "published": "false" if visibility == "draft" else "true",
                "private": "true" if visibility == "private" else "false",
                "is_created_for_kids": "true" if for_kids else "false",
            },
        )
        vid = str(self._json(r).get("id", ""))
        if not self._XID.match(vid):
            raise PlatformError("Dailymotion didn't return a video id.")
        return vid

    def status(self, video_id: str) -> dict:
        if not self._XID.match(video_id):
            raise ValueError("Invalid Dailymotion video id.")
        r = self.http.get(
            f"{self.API}/video/{video_id}",
            headers=self._auth(),
            params={"fields": "id,status,published,private,url"},
        )
        return self._json(r)


class Rumble:
    """Rumble's partner Upload API (``simple-upload.php``). Rumble issues the access token
    on request (bd@rumble.com); the video is published on upload, so the caller must have a
    person's confirmation that it may be public."""

    URL = "https://rumble.com/api/simple-upload.php"
    LICENSES = {"none": 0, "rumble_only": 6}

    def __init__(self, token: str | None, client: httpx.Client | None = None):
        if not token:
            raise MissingKey("Set RUMBLE_ACCESS_TOKEN to upload to Rumble.")
        self.token, self.http = token, _client(client)

    def upload(
        self,
        path,
        *,
        title: str,
        description: str,
        license: str,
        channel_id: str = "",
        guid: str = "",
    ) -> tuple[str, str]:
        """Returns (video id, video URL)."""
        if license not in self.LICENSES:
            raise ValueError("Unsupported Rumble license.")
        data = {
            "access_token": self.token,
            "title": title,
            "description": description,
            "license_type": str(self.LICENSES[license]),
        }
        if channel_id:
            data["channel_id"] = channel_id
        if guid:
            data["guid"] = guid
        r = self.http.post(
            self.URL,
            data=data,
            files={"video": (path.name, path.read_bytes(), "video/mp4")},
            timeout=httpx.Timeout(900.0, connect=10.0),
        )
        try:
            body = r.json()
        except ValueError:
            body = {}
        if r.status_code >= 400 or not body.get("success"):
            errors = body.get("errors") or [f"HTTP {r.status_code}"]
            raise PlatformError("Rumble: " + "; ".join(str(e) for e in errors)[:300])
        vid, url = str(body.get("video_id", "")), str(body.get("url_monetized", ""))
        if not re.fullmatch(r"[A-Za-z0-9]{2,20}", vid):
            raise PlatformError("Rumble didn't return a video id.")
        return vid, _https_host(url, ("rumble.com",)) if url else ""


def _int(value) -> int | None:
    try:
        return int(value) if value is not None and value != "" else None
    except (TypeError, ValueError):
        return None


def _float(value) -> float | None:
    try:
        return float(value) if value is not None and value != "" else None
    except (TypeError, ValueError):
        return None


class Reddit:
    """Reddit Data API, application-only OAuth (register a "script"/"web" app at
    reddit.com/prefs/apps). Reads post titles, scores and comment counts only; post bodies
    and media are never fetched or reused."""

    TOKEN_URL = "https://www.reddit.com/api/v1/access_token"  # noqa: S105  # nosec B105
    API = "https://oauth.reddit.com"
    _SUB = re.compile(r"^[A-Za-z0-9_]{2,21}$")

    def __init__(
        self,
        credentials: tuple[str, str] | None,
        user_agent: str,
        client: httpx.Client | None = None,
    ):
        if not credentials:
            raise MissingKey("Set REDDIT_CLIENT_ID and REDDIT_CLIENT_SECRET to read Reddit.")
        self.credentials, self.http = credentials, _client(client)
        self.headers = {"User-Agent": user_agent}
        self._token: str | None = None

    def _auth(self) -> dict:
        if self._token is None:
            r = self.http.post(
                self.TOKEN_URL,
                auth=self.credentials,
                headers=self.headers,
                data={"grant_type": "client_credentials"},
            )
            r.raise_for_status()
            self._token = r.json()["access_token"]
        return {**self.headers, "Authorization": f"Bearer {self._token}"}

    def listing(self, subreddit: str, sort: str = "hot", limit: int = 15) -> list[TrendSignal]:
        if not self._SUB.match(subreddit) or sort not in ("hot", "rising", "top", "new"):
            raise ValueError(f"Invalid subreddit or sort: r/{subreddit} {sort}")
        r = self.http.get(
            f"{self.API}/r/{subreddit}/{sort}",
            headers=self._auth(),
            params={"limit": min(limit, 50), "raw_json": 1},
        )
        r.raise_for_status()
        out = []
        for child in r.json().get("data", {}).get("children", []):
            d = child.get("data", {})
            if d.get("stickied") or d.get("over_18"):
                continue
            created = d.get("created_utc")
            out.append(
                TrendSignal(
                    source="reddit",
                    title=(d.get("title") or "")[:500],
                    url=f"https://www.reddit.com{d.get('permalink', '')}",
                    topic=(d.get("title") or "")[:300],
                    published_at=datetime.fromtimestamp(created, UTC) if created else None,
                    likes=int(d.get("score") or 0),
                    comments=int(d.get("num_comments") or 0),
                    snippet=(d.get("link_flair_text") or "")[:200],
                    extra={
                        "subreddit": d.get("subreddit"),
                        "upvote_ratio": d.get("upvote_ratio"),
                        "domain": d.get("domain"),
                    },
                )
            )
        return out

    def trend_signals(self, subreddits: list[str], limit: int = 15) -> list[TrendSignal]:
        seen, out = set(), []
        for sub in subreddits:
            for sort in ("rising", "hot"):
                for sig in self.listing(sub, sort, limit):
                    if sig.url not in seen:
                        seen.add(sig.url)
                        out.append(sig)
        return out


class GoogleTrends:
    """Google Trends "Trending now" RSS feed (public, no key). Each item is a rising search
    with approximate volume and related news headlines."""

    URL = "https://trends.google.com/trending/rss"
    NS = {"ht": "https://trends.google.com/trending/rss"}

    def __init__(self, client: httpx.Client | None = None):
        self.http = _client(client)

    def trending(self, geo: str = "US", limit: int = 20) -> list[TrendSignal]:
        if not re.fullmatch(r"[A-Z]{2}(-[A-Z0-9]{1,3})?", geo):
            raise ValueError(f"Invalid geo code: {geo}")
        r = self.http.get(self.URL, params={"geo": geo})
        r.raise_for_status()
        root = ElementTree.fromstring(r.content)  # defusedxml: no entity expansion / XXE
        out = []
        for item in root.iter("item"):
            query = (item.findtext("title") or "").strip()
            if not query:
                continue
            published = None
            if item.findtext("pubDate"):
                try:
                    published = parsedate_to_datetime(item.findtext("pubDate"))
                except (TypeError, ValueError):
                    published = None
            traffic = item.findtext("ht:approx_traffic", default="", namespaces=self.NS)
            news = [
                {
                    "title": (
                        n.findtext("ht:news_item_title", default="", namespaces=self.NS) or ""
                    ).strip(),
                    "url": (
                        n.findtext("ht:news_item_url", default="", namespaces=self.NS) or ""
                    ).strip(),
                }
                for n in item.findall("ht:news_item", self.NS)
            ]
            day = (published or datetime.now(UTC)).date().isoformat()
            out.append(
                TrendSignal(
                    source="google_trends",
                    title=query[:500],
                    topic=query[:300],
                    url=f"https://trends.google.com/trends/explore?q={quote(query)}&geo={geo}"
                    f"#{day}",
                    published_at=published,
                    views=_parse_traffic(traffic),
                    snippet=" · ".join(n["title"] for n in news if n["title"])[:500],
                    extra={"approx_traffic": traffic, "news": news[:5], "geo": geo},
                )
            )
            if len(out) >= limit:
                break
        return out


def _parse_traffic(text: str) -> int | None:
    """'50,000+' → 50000, '2K+' → 2000, '1M+' → 1000000."""
    m = re.match(r"\s*([\d.,]+)\s*([KkMm]?)", text or "")
    if not m:
        return None
    try:
        n = float(m.group(1).replace(",", ""))
    except ValueError:
        return None
    return int(n * {"k": 1_000, "m": 1_000_000}.get(m.group(2).lower(), 1))


class Tavily:
    """Tavily search: fresh news/web results with extracted content."""

    URL = "https://api.tavily.com/search"

    def __init__(self, api_key: str | None, client: httpx.Client | None = None):
        if not api_key:
            raise MissingKey("Set TAVILY_API_KEY to search the web.")
        self.key, self.http = api_key, _client(client)

    def search(
        self, query: str, *, topic: str = "news", days: int = 3, max_results: int = 8
    ) -> list[dict]:
        r = self.http.post(
            self.URL,
            headers={"Authorization": f"Bearer {self.key}"},
            json={
                "query": query,
                "topic": topic,
                "days": days,
                "max_results": max_results,
                "search_depth": "advanced",
                "include_answer": False,
            },
        )
        r.raise_for_status()
        return r.json().get("results", [])

    def trend_signals(self, niche: str, max_results: int = 10) -> list[TrendSignal]:
        return [
            TrendSignal(
                source="tavily",
                title=x.get("title", "")[:500],
                url=x.get("url"),
                topic=x.get("title", "")[:300],
                published_at=_parse_dt(x.get("published_date")),
                snippet=(x.get("content") or "")[:500],
                extra={"relevance": x.get("score")},
            )
            for x in self.search(f"latest {niche} news", max_results=max_results)
        ]

    def sources(self, query: str, max_results: int = 6) -> list[Source]:
        out = []
        for x in self.search(query, topic="general", days=30, max_results=max_results):
            try:
                out.append(
                    Source(
                        title=x.get("title", "")[:300],
                        url=x["url"],
                        content=(x.get("content") or "")[:4000],
                    )
                )
            except (KeyError, ValueError):
                continue
        return out


class Firecrawl:
    """Firecrawl scrape: a page's main content as clean Markdown."""

    URL = "https://api.firecrawl.dev/v1/scrape"

    def __init__(self, api_key: str | None, client: httpx.Client | None = None):
        if not api_key:
            raise MissingKey("Set FIRECRAWL_API_KEY to extract pages.")
        self.key, self.http = api_key, _client(client)

    def scrape(self, url: str) -> str:
        host = (urlsplit(url).hostname or "").lower()
        if any(h in host for h in ("youtube.com", "youtu.be", "tiktok.com", "instagram.com")):
            raise ValueError("Video platforms are read through their official APIs only.")
        r = self.http.post(
            self.URL,
            headers={"Authorization": f"Bearer {self.key}"},
            json={"url": url, "formats": ["markdown"], "onlyMainContent": True},
            timeout=httpx.Timeout(90.0, connect=10.0),
        )
        r.raise_for_status()
        return ((r.json().get("data") or {}).get("markdown") or "")[:20000]


class Pexels:
    """Pexels video search (free license, attribution appreciated)."""

    URL = "https://api.pexels.com/videos/search"
    ALLOWED_HOSTS = ("videos.pexels.com", "player.vimeo.com", "vod-progressive.akamaized.net")

    def __init__(self, api_key: str | None, client: httpx.Client | None = None):
        if not api_key:
            raise MissingKey("Set PEXELS_API_KEY for stock footage.")
        self.key, self.http = api_key, _client(client)

    def find(self, query: str, orientation: str = "portrait") -> dict | None:
        r = self.http.get(
            self.URL,
            headers={"Authorization": self.key},
            params={"query": query, "orientation": orientation, "per_page": 5, "size": "medium"},
        )
        r.raise_for_status()
        for video in r.json().get("videos", []):
            files = sorted(
                (
                    f
                    for f in video.get("video_files", [])
                    if f.get("file_type") == "video/mp4" and f.get("height")
                ),
                key=lambda f: abs((f.get("height") or 0) - 1920),
            )
            if files:
                return {
                    "url": files[0]["link"],
                    "page": video.get("url"),
                    "author": (video.get("user") or {}).get("name", ""),
                }
        return None

    def download(self, url: str, dest) -> None:
        host = (urlsplit(url).hostname or "").lower()
        if urlsplit(url).scheme != "https" or not any(
            host == h or host.endswith("." + h) for h in self.ALLOWED_HOSTS
        ):
            raise ValueError(f"Refusing to download from {host}")
        with self.http.stream(
            "GET", url, follow_redirects=True, timeout=httpx.Timeout(120.0, connect=10.0)
        ) as r:
            r.raise_for_status()
            size = 0
            with open(dest, "wb") as fh:
                for chunk in r.iter_bytes():
                    size += len(chunk)
                    if size > 200 * 1024 * 1024:
                        raise ValueError("Stock clip too large")
                    fh.write(chunk)


class ElevenLabs:
    URL = "https://api.elevenlabs.io/v1/text-to-speech/{voice}"

    def __init__(self, api_key: str | None, voice_id: str, client: httpx.Client | None = None):
        if not api_key:
            raise MissingKey("Set ELEVENLABS_API_KEY for narration.")
        self.key, self.voice, self.http = api_key, voice_id, _client(client)

    def speak(self, text: str, dest) -> None:
        r = self.http.post(
            self.URL.format(voice=self.voice),
            headers={"xi-api-key": self.key, "Accept": "audio/mpeg"},
            json={"text": text, "model_id": "eleven_multilingual_v2"},
            timeout=httpx.Timeout(120.0, connect=10.0),
        )
        r.raise_for_status()
        with open(dest, "wb") as fh:
            fh.write(r.content)
