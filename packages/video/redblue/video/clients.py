"""Thin clients for trend, research and asset APIs. Each takes an optional httpx.Client so
tests (and proxies) can inject a transport. Only official APIs and the open web are used:
nothing here downloads other creators' videos."""

from __future__ import annotations

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
