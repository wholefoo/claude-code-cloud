"""Growth agent (weekly report + prioritized improvements) and repurposing agent (drafts).

Everything these agents produce is a proposal: CMS drafts, social drafts, or builder tasks.
Nothing is published, posted or sent without a human."""

from __future__ import annotations

import json
import logging
from typing import Literal

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from redblue.cms.models import Entry
from redblue.cms.service import entry_text
from redblue.core.ai import untrusted
from redblue.core.context import Platform
from redblue.growth import analytics, freshness, linking, questions
from redblue.growth.models import GrowthReportRecord, SocialDraft

log = logging.getLogger("redblue.growth")


class Recommendation(BaseModel):
    title: str = Field(max_length=200)
    why: str = Field(max_length=600)
    action: Literal["cms_draft", "cms_edit", "pr", "config", "investigate"]
    target: str = Field(default="", max_length=500)
    priority: Literal["high", "medium", "low"] = "medium"


class GrowthReport(BaseModel):
    headline: str
    working: list[str] = Field(default_factory=list)
    losing_traffic: list[dict] = Field(default_factory=list)
    content_gaps: list[str] = Field(default_factory=list)
    recommendations: list[Recommendation] = Field(default_factory=list)


class _LLMRecs(BaseModel):
    headline: str
    working: list[str]
    recommendations: list[Recommendation]


GROWTH_SYSTEM = """You are the RedBlue growth agent. From the site's facts, write a short weekly
report and a prioritized list of concrete, white-hat improvements (better answers, fresher
content, internal links, technical fixes). Never recommend cloaking, doorway pages, fake
reviews, link schemes, bot traffic, or auto-sent outreach. Each recommendation becomes a CMS
draft or a PR that a human reviews."""


class GrowthAgent:
    def __init__(self, platform: Platform):
        self.platform = platform

    def facts(self, db: Session) -> dict:
        s = analytics.summary(db, days=7)
        trends = analytics.page_trends(db, days=7)
        return {
            "traffic_7d": {
                "pageviews": s["pageviews"],
                "visitors": s["visitors"],
                "sources": s["sources"],
                "ai_assistants": s["ai_assistants"],
            },
            "top_pages": s["top_pages"][:10],
            "losing": [
                t
                for t in trends
                if t["change"] is not None and t["change"] <= -0.25 and t["previous"] >= 10
            ][:10],
            "stale": freshness.stale_pages(db)[:15],
            "question_coverage": questions.coverage(db),
            "link_suggestions": linking.link_suggestions(db, limit=15),
            "conversions": analytics.goal_conversions(db, days=7),
        }

    def weekly_report(self, db: Session) -> GrowthReport:
        f = self.facts(db)
        gaps = [
            q["question"] for q in f["question_coverage"]["questions"] if q["status"] != "well"
        ][:15]
        recs: list[Recommendation] = []
        for q in f["question_coverage"]["questions"]:
            if q["status"] == "missing":
                recs.append(
                    Recommendation(
                        title=f"Answer “{q['question']}”",
                        action="cms_draft",
                        priority="high",
                        why="People ask this and no page answers it directly.",
                        target=q["question"],
                    )
                )
            elif q["status"] == "poorly":
                recs.append(
                    Recommendation(
                        title=f"Add a direct answer for “{q['question']}” on {q['page']}",
                        action="cms_edit",
                        priority="medium",
                        target=q["page"],
                        why="The topic is covered but there's no concise answer block.",
                    )
                )
        for page in f["losing"]:
            recs.append(
                Recommendation(
                    title=f"Investigate traffic drop on {page['path']}",
                    action="investigate",
                    priority="high",
                    target=page["path"],
                    why=f"Pageviews fell {abs(page['change']):.0%} week over week.",
                )
            )
        for st in f["stale"]:
            recs.append(
                Recommendation(
                    title=f"Refresh “{st['title']}”",
                    action="cms_edit",
                    priority="medium",
                    target=str(st["id"]),
                    why="; ".join(st["reasons"]),
                )
            )
        for ls in f["link_suggestions"][:5]:
            recs.append(
                Recommendation(
                    title=f"Link “{ls['from']}” → “{ls['to']}”",
                    action="cms_edit",
                    priority="low",
                    target=str(ls["from_id"]),
                    why="The page mentions this topic but doesn't link to it.",
                )
            )
        pv = f["traffic_7d"]["pageviews"]
        report = GrowthReport(
            headline=f"{pv} pageviews this week; question coverage "
            f"{f['question_coverage']['score']:.0%}.",
            working=[f"{p} ({n} views)" for p, n in f["top_pages"][:5]],
            losing_traffic=f["losing"],
            content_gaps=gaps,
            recommendations=recs[:25],
        )
        ai = self.platform.ai
        if ai.available:
            try:
                out = ai.structured(
                    agent="growth",
                    model=self.platform.settings.models.growth,
                    system=GROWTH_SYSTEM,
                    output=_LLMRecs,
                    task="growth.weekly",
                    prompt="Site facts:\n" + untrusted("site-facts", json.dumps(f, default=str)),
                )
                report.headline, report.working = out.headline, out.working or report.working
                report.recommendations = out.recommendations[:25] or report.recommendations
            except Exception as exc:  # noqa: BLE001 - deterministic report stands on its own
                log.warning("growth agent AI step skipped: %s", exc)
        db.add(GrowthReportRecord(report=report.model_dump(mode="json")))
        return report


class RepurposeOut(BaseModel):
    summary: str = Field(max_length=600)
    email_subject: str = Field(max_length=120)
    email_body: str = Field(max_length=4000)
    social: dict[Literal["linkedin", "x", "mastodon"], str]


class RepurposingAgent:
    def __init__(self, platform: Platform):
        self.platform = platform

    def repurpose(self, db: Session, entry: Entry) -> list[SocialDraft]:
        text = entry_text(entry)
        title = (entry.live or {}).get("title", entry.title)
        ai = self.platform.ai
        if ai.available:
            out = ai.structured(
                agent="content",
                model=self.platform.settings.models.content,
                system="Repurpose this article into a summary, an email, and social posts. "
                "Stay factual; no hype, no invented numbers. These are drafts for a "
                "human to edit.",
                prompt=untrusted("article", text[:30000]),
                output=RepurposeOut,
                task=f"repurpose:{entry.id}",
            )
        else:
            first = text.split("\n", 2)
            summary = " ".join(first[1:])[:280] if len(first) > 1 else title
            out = RepurposeOut(
                summary=summary,
                email_subject=title[:120],
                email_body=f"{title}\n\n{summary}\n\nRead more on our site.",
                social={
                    "linkedin": f"{title}: {summary[:200]}",
                    "x": title[:250],
                    "mastodon": title[:450],
                },
            )
        drafts = [
            SocialDraft(entry_id=entry.id, channel="summary", text=out.summary),
            SocialDraft(
                entry_id=entry.id,
                channel="email",
                text=f"Subject: {out.email_subject}\n\n{out.email_body}",
            ),
        ]
        drafts += [
            SocialDraft(entry_id=entry.id, channel=ch, text=t) for ch, t in out.social.items()
        ]
        db.add_all(drafts)
        db.flush()
        return drafts
