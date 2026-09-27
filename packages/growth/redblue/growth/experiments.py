"""A/B testing for headlines, CTAs and layouts, with significance reporting.

Assignment is deterministic from the (cookieless, daily) visitor hash, so a visitor sees the
same variant for the day without any cookie."""

from __future__ import annotations

import hashlib
import math
from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.orm import Session

from redblue.growth.models import Experiment, GoalEvent, PageView


def assign(experiment: Experiment, visitor: str) -> dict:
    variants = experiment.variants
    total = sum(max(0, v.get("weight", 1)) for v in variants) or 1
    bucket = int(hashlib.sha256(f"{experiment.key}:{visitor}".encode()).hexdigest()[:8], 16)
    point = bucket % total
    for v in variants:
        point -= max(0, v.get("weight", 1))
        if point < 0:
            return v
    return variants[0]


def running_for(db: Session, path: str) -> list[Experiment]:
    return [
        e
        for e in db.scalars(select(Experiment).where(Experiment.status == "running"))
        if e.path in (None, "", path)
    ]


def two_proportion_z(c1: int, n1: int, c2: int, n2: int) -> tuple[float, float]:
    """Returns (z, two-sided p-value)."""
    if min(n1, n2) == 0:
        return 0.0, 1.0
    p1, p2 = c1 / n1, c2 / n2
    p = (c1 + c2) / (n1 + n2)
    se = math.sqrt(p * (1 - p) * (1 / n1 + 1 / n2))
    if se == 0:
        return 0.0, 1.0
    z = (p2 - p1) / se
    return z, math.erfc(abs(z) / math.sqrt(2))


def results(db: Session, experiment: Experiment, alpha: float = 0.05) -> dict:
    exposed: dict[str, set] = defaultdict(set)
    for visitor, day, variants in db.execute(
        select(PageView.visitor, PageView.day, PageView.variants)
    ):
        v = (variants or {}).get(experiment.key)
        if v:
            exposed[v].add((visitor, day))
    converted: dict[str, set] = defaultdict(set)
    for visitor, day, variants in db.execute(
        select(GoalEvent.visitor, GoalEvent.day, GoalEvent.variants).where(
            GoalEvent.goal == experiment.goal
        )
    ):
        v = (variants or {}).get(experiment.key)
        if v and (visitor, day) in exposed[v]:
            converted[v].add((visitor, day))
    rows = []
    control = experiment.variants[0]["key"]
    n0, c0 = len(exposed[control]), len(converted[control])
    for var in experiment.variants:
        k = var["key"]
        n, c = len(exposed[k]), len(converted[k])
        row = {"variant": k, "visitors": n, "conversions": c, "rate": round(c / n, 4) if n else 0.0}
        if k != control:
            z, p = two_proportion_z(c0, n0, c, n)
            row.update(
                {
                    "z": round(z, 3),
                    "p_value": round(p, 4),
                    "significant": p < alpha and min(n, n0) >= 100,
                    "lift": round((row["rate"] - (c0 / n0)) / (c0 / n0), 3) if n0 and c0 else None,
                }
            )
        rows.append(row)
    return {
        "experiment": experiment.key,
        "goal": experiment.goal,
        "variants": rows,
        "note": "Significance requires p < 0.05 and at least 100 visitors per variant.",
    }
