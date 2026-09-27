"""Privacy-friendly, cookieless analytics.

Visitors are counted with a hash of (daily salt, IP, user agent, host). Salts rotate daily
and are deleted, so nobody (including the site owner) can follow a visitor across days. No
cookies, no local storage, no raw IPs stored, Do-Not-Track respected, bots ignored.
"""

from __future__ import annotations

import hashlib
import re
import secrets
from collections import Counter, defaultdict
from datetime import date, timedelta
from urllib.parse import parse_qs, urlsplit

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from redblue.core.db import utcnow
from redblue.growth.models import DailySalt, GoalEvent, PageView

SEARCH_ENGINES = (
    "google.",
    "bing.com",
    "duckduckgo.com",
    "search.yahoo.",
    "ecosia.org",
    "search.brave.com",
    "yandex.",
    "baidu.com",
    "startpage.com",
    "qwant.com",
    "kagi.com",
)
SOCIAL = (
    "facebook.com",
    "t.co",
    "twitter.com",
    "x.com",
    "linkedin.com",
    "lnkd.in",
    "reddit.com",
    "news.ycombinator.com",
    "youtube.com",
    "instagram.com",
    "threads.net",
    "bsky.app",
    "mastodon.",
    "pinterest.",
    "tiktok.com",
)
AI_ASSISTANTS = {
    "chatgpt.com": "ChatGPT",
    "chat.openai.com": "ChatGPT",
    "perplexity.ai": "Perplexity",
    "claude.ai": "Claude",
    "gemini.google.com": "Gemini",
    "copilot.microsoft.com": "Copilot",
    "you.com": "You.com",
    "phind.com": "Phind",
    "chat.mistral.ai": "Mistral",
    "meta.ai": "Meta AI",
    "grok.com": "Grok",
    "chat.deepseek.com": "DeepSeek",
}
AI_UTM_SOURCES = {"chatgpt.com": "ChatGPT", "perplexity": "Perplexity", "claude": "Claude"}
BOT_UA = re.compile(
    r"bot|crawl|spider|slurp|headless|lighthouse|preview|monitor|curl|wget|"
    r"python-requests|httpx|scrapy",
    re.I,
)


def daily_salt(db: Session, day: date | None = None) -> str:
    day = day or utcnow().date()
    row = db.get(DailySalt, day)
    if row is None:
        row = DailySalt(day=day, salt=secrets.token_hex(32))
        db.add(row)
        db.flush()
        db.execute(delete(DailySalt).where(DailySalt.day < day - timedelta(days=1)))
    return row.salt


def visitor_hash(db: Session, ip: str, user_agent: str, host: str) -> str:
    raw = f"{daily_salt(db)}|{ip}|{user_agent}|{host}".encode()
    return hashlib.sha256(raw).hexdigest()[:32]


def classify(referrer: str, query: str, own_host: str) -> dict:
    """Returns source, referrer_host, ai_assistant and utm fields."""
    params = {k: v[0][:200] for k, v in parse_qs(query.lstrip("?")).items() if v}
    utm = {k: params.get(f"utm_{k}", "") for k in ("source", "medium", "campaign")}
    host = (urlsplit(referrer).hostname or "").lower().removeprefix("www.") if referrer else ""
    if host == own_host.removeprefix("www."):
        host = ""
    ai = next(
        (name for dom, name in AI_ASSISTANTS.items() if host == dom or host.endswith("." + dom)), ""
    )
    if not ai and utm["source"]:
        ai = next((n for k, n in AI_UTM_SOURCES.items() if k in utm["source"].lower()), "")
    if ai:
        source = "ai"
    elif utm["medium"].lower() in ("email", "newsletter"):
        source = "email"
    elif utm["medium"].lower() in ("cpc", "ppc", "paid", "paidsocial"):
        source = "paid"
    elif utm["medium"].lower() == "social" or any(
        host == s or host.endswith("." + s) or host.startswith(s) for s in SOCIAL
    ):
        source = "social"
    elif any(s in host for s in SEARCH_ENGINES):
        source = "search"
    elif host:
        source = "referral"
    elif utm["source"]:
        source = "campaign"
    else:
        source = "direct"
    return {
        "source": source,
        "referrer_host": host[:255],
        "ai_assistant": ai,
        "utm_source": utm["source"],
        "utm_medium": utm["medium"],
        "utm_campaign": utm["campaign"],
    }


def device_class(user_agent: str) -> str:
    ua = user_agent.lower()
    if "ipad" in ua or "tablet" in ua:
        return "tablet"
    return "mobile" if "mobi" in ua or "android" in ua else "desktop"


def is_bot(user_agent: str) -> bool:
    return not user_agent or bool(BOT_UA.search(user_agent))


_SAFE_PATH = re.compile(r"^/[^\s<>\"']{0,499}$")


def record(
    db: Session, payload: dict, *, ip: str, user_agent: str, host: str, dnt: bool = False
) -> bool:
    if dnt or is_bot(user_agent):
        return False
    path = str(payload.get("p", ""))[:500]
    if not _SAFE_PATH.match(path) or path.startswith(("/admin", "/_rb", "/api")):
        return False
    variants = payload.get("e") if isinstance(payload.get("e"), dict) else {}
    variants = {str(k)[:80]: str(v)[:40] for k, v in list(variants.items())[:10]}
    visitor = visitor_hash(db, ip, user_agent, host)
    now = utcnow()
    if payload.get("t") == "goal":
        goal = re.sub(r"[^a-z0-9_:-]", "", str(payload.get("g", "")).lower())[:100]
        if not goal:
            return False
        db.add(
            GoalEvent(
                ts=now, day=now.date(), goal=goal, path=path, visitor=visitor, variants=variants
            )
        )
        return True
    info = classify(str(payload.get("r", ""))[:1000], str(payload.get("q", ""))[:1000], host)
    db.add(
        PageView(
            ts=now,
            day=now.date(),
            path=path,
            visitor=visitor,
            device=device_class(user_agent),
            variants=variants,
            **info,
        )
    )
    return True


# ---------------------------------------------------------------- reports


def summary(db: Session, days: int = 30) -> dict:
    since = utcnow() - timedelta(days=days)
    rows = db.execute(
        select(
            PageView.path,
            PageView.visitor,
            PageView.source,
            PageView.ai_assistant,
            PageView.referrer_host,
            PageView.day,
        ).where(PageView.ts >= since)
    ).all()
    by_day: dict[str, set] = defaultdict(set)
    for r in rows:
        by_day[r.day.isoformat()].add(r.visitor)
    return {
        "pageviews": len(rows),
        "visitors": sum(len(v) for v in by_day.values()),  # daily-unique, summed
        "sources": Counter(r.source for r in rows).most_common(),
        "top_pages": Counter(r.path for r in rows).most_common(20),
        "ai_assistants": Counter(r.ai_assistant for r in rows if r.ai_assistant).most_common(),
        "referrers": Counter(r.referrer_host for r in rows if r.referrer_host).most_common(20),
        "daily": sorted((d, len(v)) for d, v in by_day.items()),
    }


def goal_conversions(db: Session, days: int = 30) -> list[dict]:
    since = utcnow() - timedelta(days=days)
    visitor_days = set(
        db.execute(select(PageView.visitor, PageView.day).where(PageView.ts >= since)).all()
    )
    converters: dict[str, set] = defaultdict(set)
    for goal, visitor, day in db.execute(
        select(GoalEvent.goal, GoalEvent.visitor, GoalEvent.day).where(GoalEvent.ts >= since)
    ):
        converters[goal].add((visitor, day))
    total = len(visitor_days)
    return [
        {"goal": g, "converters": len(v), "rate": round(len(v) / total, 4) if total else 0.0}
        for g, v in sorted(converters.items())
    ]


def funnel(db: Session, steps: list[str], days: int = 30) -> list[dict]:
    """Steps are paths (``/pricing``) or goals (``goal:signup``). Counts visitor-days that
    completed each step in order."""
    since = utcnow() - timedelta(days=days)
    events: dict[tuple, list[tuple]] = defaultdict(list)
    for r in db.execute(
        select(PageView.visitor, PageView.day, PageView.ts, PageView.path).where(
            PageView.ts >= since
        )
    ):
        events[(r.visitor, r.day)].append((r.ts, r.path))
    for r in db.execute(
        select(GoalEvent.visitor, GoalEvent.day, GoalEvent.ts, GoalEvent.goal).where(
            GoalEvent.ts >= since
        )
    ):
        events[(r.visitor, r.day)].append((r.ts, f"goal:{r.goal}"))
    counts = [0] * len(steps)
    for evs in events.values():
        evs.sort()
        i = 0
        for _, name in evs:
            if i < len(steps) and name == steps[i]:
                counts[i] += 1
                i += 1
    out = []
    for i, step in enumerate(steps):
        prev = counts[i - 1] if i else counts[0]
        out.append(
            {
                "step": step,
                "count": counts[i],
                "rate_from_previous": round(counts[i] / prev, 4) if prev else 0.0,
            }
        )
    return out


def page_trends(db: Session, days: int = 7) -> list[dict]:
    """Pageviews this period vs the previous one, per page (for 'pages losing traffic')."""
    now = utcnow()
    cur_start, prev_start = now - timedelta(days=days), now - timedelta(days=2 * days)
    cur = Counter(p for (p,) in db.execute(select(PageView.path).where(PageView.ts >= cur_start)))
    prev = Counter(
        p
        for (p,) in db.execute(
            select(PageView.path).where(PageView.ts >= prev_start, PageView.ts < cur_start)
        )
    )
    rows = []
    for path in set(cur) | set(prev):
        c, p = cur[path], prev[path]
        rows.append(
            {
                "path": path,
                "current": c,
                "previous": p,
                "change": round((c - p) / p, 3) if p else None,
            }
        )
    return sorted(rows, key=lambda r: r["change"] if r["change"] is not None else 99)
