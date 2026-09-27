"""Seed a new site with an admin user and starter content (all published, all editable)."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from redblue.cms import service as cms
from redblue.cms.models import Entry
from redblue.core.auth import Role, User, create_user

STARTER: list[tuple[str, dict]] = [
    (
        "page",
        {
            "title": "Build something people can find",
            "slug": "home",
            "summary": "A fast, secure website built with RedBlue.",
            "tags": [],
            "data": {"page_type": "home"},
            "blocks": [
                {
                    "type": "answer",
                    "question": "What do we do?",
                    "answer": "We help small teams launch fast, secure websites that search "
                    "engines and AI assistants can understand.",
                },
                {
                    "type": "features",
                    "heading": "Why it works",
                    "items": [
                        {
                            "title": "Secure by default",
                            "text": "Every change passes an adversarial security gate.",
                        },
                        {
                            "title": "Built to be found",
                            "text": "Structured data, sitemaps and answer-first content.",
                        },
                        {
                            "title": "Yours to keep",
                            "text": "Plain FastAPI code you can eject any time.",
                        },
                    ],
                },
                {
                    "type": "cta",
                    "heading": "Ready to talk?",
                    "text": "Tell us what you need.",
                    "button_label": "Contact us",
                    "button_url": "/contact",
                },
                {
                    "type": "form",
                    "kind": "newsletter",
                    "heading": "Get updates",
                    "button_label": "Subscribe",
                },
            ],
        },
    ),
    (
        "page",
        {
            "title": "About us",
            "slug": "about",
            "tags": ["nav"],
            "summary": "Who we are and why we started.",
            "data": {"page_type": "about"},
            "blocks": [{"type": "paragraph", "text": "Replace this with your story."}],
        },
    ),
    (
        "page",
        {
            "title": "Pricing",
            "slug": "pricing",
            "tags": ["nav"],
            "summary": "Simple plans for every stage.",
            "data": {"page_type": "pricing"},
            "blocks": [
                {
                    "type": "pricing",
                    "plans": [
                        {
                            "name": "Starter",
                            "price": "$0",
                            "features": ["1 site", "Community support"],
                        },
                        {
                            "name": "Team",
                            "price": "$29",
                            "highlighted": True,
                            "features": ["5 sites", "Priority support"],
                            "cta_label": "Start trial",
                        },
                    ],
                },
                {
                    "type": "faq",
                    "items": [
                        {
                            "question": "Can I cancel any time?",
                            "answer": "Yes. Plans are month to month.",
                        }
                    ],
                },
            ],
        },
    ),
    (
        "page",
        {
            "title": "Contact",
            "slug": "contact",
            "tags": ["nav"],
            "summary": "Get in touch with our team.",
            "data": {"page_type": "contact"},
            "blocks": [{"type": "paragraph", "text": "We usually reply within a day."}],
        },
    ),
    (
        "page",
        {
            "title": "Thank you",
            "slug": "thank-you",
            "summary": "Thanks for reaching out.",
            "data": {"page_type": "thank_you"},
            "seo": {"noindex": True},
            "blocks": [{"type": "paragraph", "text": "We'll be in touch soon."}],
        },
    ),
    (
        "page",
        {
            "title": "Privacy policy",
            "slug": "privacy",
            "data": {"page_type": "privacy"},
            "summary": "How we handle your data.",
            "blocks": [
                {"type": "heading", "text": "What we collect"},
                {
                    "type": "paragraph",
                    "text": "This site uses cookieless analytics "
                    "and stores no personal data unless you submit a form.",
                },
            ],
        },
    ),
    (
        "post",
        {
            "title": "Hello, world",
            "summary": "Our first post.",
            "tags": ["news"],
            "seo": {"target_questions": ["What is this blog about?"]},
            "blocks": [
                {
                    "type": "answer",
                    "question": "What is this blog about?",
                    "answer": "Practical notes on building secure, findable websites.",
                },
                {"type": "paragraph", "text": "Edit or delete this post in the admin."},
            ],
        },
    ),
    (
        "glossary",
        {
            "title": "Answer engine optimization",
            "summary": "Making content easy for AI assistants to cite.",
            "data": {
                "short_definition": "Structuring content so answer engines and AI "
                "assistants can find, understand and cite it."
            },
            "blocks": [
                {
                    "type": "paragraph",
                    "text": "AEO complements SEO by leading "
                    "with direct answers and clear structured data.",
                }
            ],
        },
    ),
]


def ensure_admin(db: Session, email: str, password: str) -> User:
    user = db.scalar(select(User).where(User.email == email.lower()))
    return user or create_user(db, email, password, Role.admin, "Admin")


def seed(db: Session, admin: User) -> int:
    n = 0
    for collection, data in STARTER:
        slug = data.get("slug") or cms.slugify(data["title"])
        if db.scalar(select(Entry.id).where(Entry.collection == collection, Entry.slug == slug)):
            continue
        e = cms.create_entry(db, collection, cms.EntryInput(**data), admin)
        cms.submit_for_review(db, e, admin)
        cms.approve(db, e, admin)
        cms.publish(db, e, admin)
        n += 1
    return n
