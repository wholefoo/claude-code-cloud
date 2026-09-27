import pytest

from redblue.cms import service
from redblue.cms.models import Redirect, Status
from redblue.core.auth import Role


def _input(**kw):
    base = {
        "title": "Hello world",
        "summary": "A page",
        "blocks": [{"type": "paragraph", "text": "Body text"}],
    }
    base.update(kw)
    return service.EntryInput(**base)


def test_full_workflow_and_live_snapshot(db, users):
    w, ed, pub = users[Role.writer], users[Role.editor], users[Role.publisher]
    e = service.create_entry(db, "post", _input(), w)
    assert e.status == Status.draft and e.slug == "hello-world" and not e.is_live

    with pytest.raises(service.PermissionDenied):
        service.approve(db, e, w)
    service.submit_for_review(db, e, w)
    with pytest.raises(service.PermissionDenied):
        service.update_entry(db, e, _input(title="Hijack"), w)  # writers can't edit in review
    with pytest.raises(service.PermissionDenied):
        service.publish(db, e, ed)  # editors can't publish
    service.approve(db, e, ed)
    service.publish(db, e, pub)
    assert e.status == Status.published and e.live["title"] == "Hello world"
    assert service.live_entry(db, "post", "hello-world").id == e.id

    # Editing a published entry doesn't change the live page until re-published.
    service.update_entry(db, e, _input(title="Hello again", slug="hello-again"), ed)
    assert e.status == Status.draft and e.live["title"] == "Hello world"
    service.submit_for_review(db, e, ed)
    service.approve(db, e, ed)
    service.publish(db, e, pub)
    assert e.live["title"] == "Hello again"
    r = service.resolve_redirect(db, "/blog/hello-world")
    assert r.to_path == "/blog/hello-again" and r.automatic and r.status_code == 301

    revs = service.revisions(db, e.id)
    assert len(revs) == 2
    service.rollback(db, e, 1, ed)
    assert e.title == "Hello world" and e.status == Status.draft


def test_agents_only_create_drafts(db, users):
    e = service.create_entry(db, "post", _input(), None, agent="content")
    assert e.ai_generated and e.status == Status.draft
    with pytest.raises(service.PermissionDenied):
        service.publish(db, e, None)


def test_publish_checks_block(db, users):
    ed, pub = users[Role.editor], users[Role.publisher]

    def no_todo(_db, entry):
        return ["Contains TODO"] if "TODO" in service.entry_text(entry, live=False) else []

    service.register_publish_check(no_todo)
    try:
        e = service.create_entry(db, "post", _input(summary="TODO fix"), ed)
        service.submit_for_review(db, e, ed)
        service.approve(db, e, ed)
        with pytest.raises(service.PublishBlocked) as exc:
            service.publish(db, e, pub)
        assert exc.value.problems == ["Contains TODO"]
    finally:
        service._publish_checks.remove(no_todo)


def test_scheduled_publish(db, users):
    from datetime import timedelta

    from redblue.core.db import utcnow

    ed, pub = users[Role.editor], users[Role.publisher]
    e = service.create_entry(db, "post", _input(), ed)
    service.submit_for_review(db, e, ed)
    service.approve(db, e, ed)
    service.publish(db, e, pub, at=utcnow() + timedelta(hours=1))
    assert e.status == Status.scheduled and not e.is_live
    assert service.publish_due(db) == 0
    e.publish_at = utcnow() - timedelta(seconds=1)
    assert service.publish_due(db) == 1 and e.is_live


def test_slug_uniqueness_and_search(db, users):
    ed, pub = users[Role.editor], users[Role.publisher]
    a = service.create_entry(db, "post", _input(), ed)
    b = service.create_entry(db, "post", _input(), ed)
    assert b.slug == "hello-world-2"
    for e in (a,):
        service.submit_for_review(db, e, ed)
        service.approve(db, e, ed)
        service.publish(db, e, pub)
    assert [x.id for x in service.search(db, "body")] == [a.id]


def test_redirect_chains_collapse(db):
    service.add_redirect(db, "/a", "/b")
    service.add_redirect(db, "/b", "/c")
    assert service.resolve_redirect(db, "/a").to_path == "/c"
    with pytest.raises(service.WorkflowError):
        service.add_redirect(db, "/x", "javascript:alert(1)")
    assert db.query(Redirect).count() == 2
