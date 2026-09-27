from datetime import timedelta

import pytest

from redblue.cms import service
from redblue.core.auth import Role, create_user
from redblue.core.db import Database, utcnow
from redblue.growth import analytics, freshness, linking, quality, questions, utm
from redblue.growth.audit import audit_html
from redblue.growth.experiments import assign
from redblue.growth.models import Experiment


@pytest.fixture
def db():
    d = Database("sqlite://")
    d.create_all()
    with d.session() as s:
        yield s


def _publish(db, admin, collection, title, blocks, **kw):
    e = service.create_entry(
        db, collection, service.EntryInput(title=title, blocks=blocks, **kw), admin
    )
    service.submit_for_review(db, e, admin)
    service.approve(db, e, admin)
    service.publish(db, e, admin)
    return e


@pytest.mark.parametrize(
    "ref,query,source,ai",
    [
        ("https://chatgpt.com/", "", "ai", "ChatGPT"),
        ("https://www.perplexity.ai/search", "", "ai", "Perplexity"),
        ("https://claude.ai/chat/x", "", "ai", "Claude"),
        ("https://www.google.com/", "", "search", ""),
        ("https://t.co/abc", "", "social", ""),
        ("", "?utm_source=weekly&utm_medium=email", "email", ""),
        ("https://blog.example.org/x", "", "referral", ""),
        ("", "", "direct", ""),
        ("", "?utm_source=chatgpt.com", "ai", "ChatGPT"),
    ],
)
def test_source_classification(ref, query, source, ai):
    c = analytics.classify(ref, query, "mysite.com")
    assert c["source"] == source and c["ai_assistant"] == ai


def test_visitor_hash_rotates_daily(db):
    a = analytics.visitor_hash(db, "1.2.3.4", "UA", "h")
    assert a == analytics.visitor_hash(db, "1.2.3.4", "UA", "h")
    assert a != analytics.visitor_hash(db, "1.2.3.5", "UA", "h")
    salt_today = analytics.daily_salt(db)
    tomorrow = utcnow().date() + timedelta(days=1)
    assert analytics.daily_salt(db, tomorrow) != salt_today


def test_quality_blocks_duplicates_and_todos(db):
    admin = create_user(db, "a@example.com", None, Role.admin)
    body = " ".join(f"word{i}" for i in range(200))
    _publish(
        db,
        admin,
        "location",
        "Plumber A",
        [{"type": "paragraph", "text": body}],
        data={"name": "A", "city": "Springfield"},
    )
    dup = service.create_entry(
        db,
        "location",
        service.EntryInput(
            title="Plumber B",
            blocks=[{"type": "paragraph", "text": body}],
            data={"name": "B", "city": "Shelbyville"},
        ),
        admin,
    )
    probs = quality.quality_problems(db, dup, min_words=150)
    assert any("Near-duplicate" in p for p in probs)
    todo = service.create_entry(
        db,
        "post",
        service.EntryInput(
            title="T", blocks=[{"type": "paragraph", "text": "TODO(editor): add facts"}]
        ),
        admin,
    )
    assert any("TODO" in p for p in quality.quality_problems(db, todo))


def test_linking_questions_freshness(db):
    admin = create_user(db, "a@example.com", None, Role.admin)
    a = _publish(
        db,
        admin,
        "post",
        "Brewing coffee at home",
        [
            {
                "type": "answer",
                "question": "How do I brew coffee at home?",
                "answer": "Use freshly ground beans and water just off the boil.",
            },
            {"type": "paragraph", "text": "Coffee grinders matter for brewing coffee."},
        ],
    )
    _publish(
        db,
        admin,
        "post",
        "Coffee grinders",
        [
            {
                "type": "paragraph",
                "text": "Burr grinders give an even grind for coffee "
                "brewing at home. See Brewing coffee at home.",
            }
        ],
    )
    rel = linking.related(db, a)
    assert rel and rel[0]["title"] == "Coffee grinders"
    sugg = linking.link_suggestions(db)
    assert any(s["to"] == "Brewing coffee at home" for s in sugg)
    cov = questions.coverage(
        db, ["How do I brew coffee at home?", "What is a burr grinder?", "How to repair a bicycle?"]
    )
    status = {q["question"]: q["status"] for q in cov["questions"]}
    assert status["How do I brew coffee at home?"] == "well"
    assert status["How to repair a bicycle?"] == "missing"
    a.content_updated_at = utcnow() - timedelta(days=500)
    assert any(s["id"] == a.id for s in freshness.stale_pages(db))


def test_experiment_assignment_is_stable_and_weighted():
    e = Experiment(
        key="k", name="n", goal="g", variants=[{"key": "a", "weight": 1}, {"key": "b", "weight": 3}]
    )
    picks = [assign(e, f"visitor{i}")["key"] for i in range(2000)]
    assert assign(e, "visitor1") == assign(e, "visitor1")
    assert 0.65 < picks.count("b") / len(picks) < 0.85


def test_utm_builder():
    url = utm.build_utm_url("https://x.com/p?a=1", source="news", medium="email", campaign="spring")
    assert "a=1" in url and "utm_campaign=spring" in url
    with pytest.raises(ValueError):
        utm.build_utm_url("javascript:alert(1)", source="a", medium="b", campaign="c")
    with pytest.raises(ValueError):
        utm.build_utm_url("https://x.com", source="a b", medium="b", campaign="c")


def test_audit_html_flags_problems():
    page = audit_html(
        "/x",
        200,
        "<html><head><title>Hi</title></head><body><h1>a</h1>"
        "<h1>b</h1><img src=a.png></body></html>",
    )
    checks = {i.check for i in page.issues}
    assert {"title", "h1", "lang", "img-alt", "canonical", "structured-data"} <= checks
