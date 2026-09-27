from __future__ import annotations

from datetime import timedelta

import pytest
from redblue.core.ai import AgentCall
from redblue.core.db import utcnow
from redblue.observe import metrics
from redblue.observe.models import RequestMetric


def test_percentile_math():
    data = list(range(1, 11))
    assert metrics.percentile(data, 50) == 5.5
    assert metrics.percentile(data, 95) == pytest.approx(9.55)
    assert metrics.percentile(data, 99) == pytest.approx(9.91)
    assert metrics.percentile(data, 0) == 1
    assert metrics.percentile(data, 100) == 10
    assert metrics.percentile([3, 1, 2], 50) == 2  # unsorted input
    assert metrics.percentile([42], 99) == 42
    assert metrics.percentile([], 50) is None
    with pytest.raises(ValueError):
        metrics.percentile(data, 101)


def _add(db, rows):
    with db.session() as s:
        for ts, method, route, status, ms in rows:
            s.add(RequestMetric(ts=ts, method=method, route=route, status=status, duration_ms=ms))


def test_route_stats_and_error_rate(db):
    now = utcnow()
    rows = [(now - timedelta(minutes=i), "GET", "/a", 200, float(i + 1)) for i in range(10)]
    rows += [(now - timedelta(minutes=1), "GET", "/b", 500, 50.0)]
    rows += [(now - timedelta(minutes=2), "GET", "/b", 200, 10.0)]
    rows += [(now - timedelta(days=3), "GET", "/a", 200, 9999.0)]  # outside window
    _add(db, rows)
    stats = metrics.route_stats(db, window=timedelta(hours=24))
    a, b = stats
    assert (a["route"], a["count"], a["p50_ms"], a["p95_ms"], a["error_rate"]) == (
        "/a",
        10,
        5.5,
        9.55,
        0.0,
    )
    assert (b["route"], b["count"], b["errors"], b["error_rate"]) == ("/b", 2, 1, 0.5)


def test_timeseries_buckets(db):
    now = utcnow().replace(microsecond=0)
    _add(
        db,
        [
            (now - timedelta(minutes=90), "GET", "/a", 200, 10.0),
            (now - timedelta(minutes=30), "GET", "/a", 200, 20.0),
            (now - timedelta(minutes=20), "GET", "/a", 500, 30.0),
        ],
    )
    series = metrics.timeseries(db, since=now - timedelta(hours=2), until=now)
    assert [b["count"] for b in series] == [1, 2]
    assert series[1]["errors"] == 1
    only = metrics.timeseries(db, since=now - timedelta(hours=2), until=now, route="/zzz")
    assert [b["count"] for b in only] == [0, 0]


def test_compare_windows(db):
    now = utcnow()
    before = (now - timedelta(hours=2), now - timedelta(hours=1))
    after = (now - timedelta(hours=1), now)
    _add(db, [(now - timedelta(minutes=90), "GET", "/p", 200, 100.0) for _ in range(5)])
    _add(db, [(now - timedelta(minutes=30), "GET", "/p", 200, 250.0) for _ in range(5)])
    (row,) = metrics.compare_windows(db, before, after)
    assert row["p95_ratio"] == 2.5
    assert row["before"]["count"] == 5 and row["after"]["count"] == 5


def test_agent_cost_summary(app, db):
    with db.session() as s:
        s.add_all(
            [
                AgentCall(
                    agent="content",
                    model="m",
                    task="draft",
                    page="/blog/a",
                    input_tokens=100,
                    output_tokens=50,
                    cost_usd=0.5,
                ),
                AgentCall(
                    agent="content",
                    model="m",
                    task="draft",
                    page="/blog/b",
                    input_tokens=200,
                    output_tokens=10,
                    cost_usd=0.25,
                ),
                AgentCall(
                    agent="ops",
                    model="m",
                    task="explain:latency_regression",
                    page="/pricing",
                    input_tokens=10,
                    output_tokens=5,
                    cost_usd=0.01,
                ),
                AgentCall(
                    agent="ops",
                    model="m",
                    task="old",
                    input_tokens=1,
                    output_tokens=1,
                    cost_usd=9.0,
                    created_at=utcnow() - timedelta(days=60),
                ),
            ]
        )
    out = metrics.agent_costs(db, days=30)
    assert out["total_cost_usd"] == 0.76
    assert out["total_calls"] == 3
    by_agent = {r["agent"]: r for r in out["by_agent"]}
    assert by_agent["content"]["cost_usd"] == 0.75
    assert by_agent["content"]["input_tokens"] == 300
    assert {r["key"]: r["calls"] for r in out["by_task"]} == {
        "draft": 2,
        "explain:latency_regression": 1,
    }
    assert [r["key"] for r in out["by_page"]] == ["/blog/a", "/blog/b", "/pricing"]
    # Same numbers via core's AIClient.cost_summary.
    via_ai = metrics.agent_costs(db, days=30, ai=app.state.rb.ai)
    assert via_ai["total_cost_usd"] == 0.76
    assert via_ai["by_agent"][0]["agent"] == "content"
