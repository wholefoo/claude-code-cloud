"""Thin clients for trend, research and asset APIs. Each takes an optional httpx.Client so
tests (and proxies) can inject a transport. Only official APIs and the open web are used:
nothing here downloads other creators' videos."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

import httpx

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
