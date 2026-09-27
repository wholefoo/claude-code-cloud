import re

from sqlalchemy import select

from redblue.growth.models import Experiment, PageView, Subscriber


def test_newsletter_double_opt_in(app, client):
    r = client.post(
        "/_rb/forms/newsletter",
        data={"csrf_token": csrf(client), "email": "Reader@Example.com", "consent": "yes"},
        headers={"hx-request": "true"},
    )
    assert "check your inbox" in r.text
    outbox = app.state.rb.email.outbox
    link = re.search(r"http://testserver(/newsletter/confirm/\S+)", outbox[-1].text).group(1)
    with app.state.rb.db.session() as db:
        sub = db.scalar(select(Subscriber))
        assert sub.status == "pending" and sub.consent_text and sub.email == "reader@example.com"
    assert client.get(link).status_code == 200
    with app.state.rb.db.session() as db:
        assert db.scalar(select(Subscriber)).status == "confirmed"
    assert client.get("/newsletter/confirm/forged").status_code == 400


def test_form_validation_consent_and_honeypot(app, client):
    r = client.post(
        "/_rb/forms/newsletter",
        data={"csrf_token": csrf(client), "email": "a@b.co"},
        headers={"hx-request": "true"},
    )
    assert r.status_code == 422 and "consent" in r.text
    before = len(app.state.rb.email.outbox)
    r = client.post(
        "/_rb/forms/contact",
        data={"csrf_token": csrf(client), "email": "a@b.co", "message": "hi", "website": "spam"},
        headers={"hx-request": "true"},
    )
    assert r.status_code == 200 and len(app.state.rb.email.outbox) == before


def test_contact_form_notifies_owner(app, client):
    r = client.post(
        "/_rb/forms/contact",
        data={
            "csrf_token": csrf(client),
            "name": "Ann",
            "email": "ann@example.com",
            "message": "Hello",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303 and r.headers["location"] == "/thank-you"
    assert app.state.rb.email.outbox[-1].to == "owner@example.com"


def test_beacon_cookieless_ai_referrals(app, client):
    ua = {"user-agent": "Mozilla/5.0 (X11; Linux x86_64) Firefox/130.0"}
    client.post(
        "/_rb/beacon",
        json={"t": "pageview", "p": "/blog/hello-world", "r": "https://chatgpt.com/", "q": ""},
        headers=ua,
    )
    client.post(
        "/_rb/beacon",
        json={"t": "pageview", "p": "/", "r": "https://www.google.com/", "q": "?utm_source=x"},
        headers=ua,
    )
    client.post(
        "/_rb/beacon", json={"t": "pageview", "p": "/"}, headers={"user-agent": "Googlebot/2.1"}
    )
    client.post("/_rb/beacon", json={"t": "pageview", "p": "/"}, headers={**ua, "dnt": "1"})
    client.post("/_rb/beacon", json={"t": "pageview", "p": "/admin"}, headers=ua)
    with app.state.rb.db.session() as db:
        rows = list(db.scalars(select(PageView)))
    assert sorted(r.source for r in rows) == ["ai", "search"]
    assert rows[0].ai_assistant == "ChatGPT"
    assert all(len(r.visitor) == 32 for r in rows)
    assert "set-cookie" not in client.post("/_rb/beacon", json={"p": "/"}, headers=ua).headers


def test_experiment_assignment_and_results(app, client):
    with app.state.rb.db.session() as db:
        db.add(
            Experiment(
                key="hero_headline",
                name="Hero",
                goal="signup",
                status="running",
                variants=[
                    {"key": "v0", "value": "Control headline", "weight": 1},
                    {"key": "v1", "value": "Challenger headline", "weight": 1},
                ],
            )
        )
    html = client.get("/").text
    assert ("Control headline" in html) or ("Challenger headline" in html)
    assert (
        "data-rb-exp='{&#34;hero_headline&#34;" in html or 'data-rb-exp=\'{"hero_headline"' in html
    )
    from redblue.growth.experiments import two_proportion_z

    z, p = two_proportion_z(100, 1000, 150, 1000)
    assert z > 3 and p < 0.001


def csrf(client):
    return client.cookies["rb_csrf"]
