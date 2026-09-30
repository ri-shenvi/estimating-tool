"""Soft-key evidence scoring for candidate opportunities (SPEC-03 F2)."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime

from rapidfuzz import fuzz

from bidtriage.resolution.normalize import normalize_domain, normalize_name

AUTO_MERGE = 0.9
REVIEW = 0.6


@dataclass
class Candidate:
    opportunity_id: str
    project_name: str | None
    gc_id: str | None
    gc_domain: str | None
    city: str | None
    lat: float | None
    lon: float | None
    bid_due: datetime | None
    owner_name: str | None
    project_number: str | None = None
    platform_ids: set[str] = field(default_factory=set)
    thread_ids: set[str] = field(default_factory=set)


@dataclass
class Incoming:
    project_name: str | None
    gc_id: str | None
    gc_domain: str | None
    city: str | None
    lat: float | None
    lon: float | None
    bid_due: datetime | None
    owner_name: str | None
    project_number: str | None = None
    platform_ids: set[str] = field(default_factory=set)
    thread_ids: set[str] = field(default_factory=set)
    kind: str = "itb"


@dataclass
class Evidence:
    score: float
    hard_key: str | None
    components: dict[str, float]
    notes: list[str]
    same_gc: bool


def _km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def name_similarity(a: str | None, b: str | None) -> float:
    na, nb = normalize_name(a), normalize_name(b)
    if not na or not nb:
        return 0.0
    return max(fuzz.token_set_ratio(na, nb), fuzz.ratio(na, nb)) / 100.0


def same_gc(c: Candidate, i: Incoming) -> bool:
    if c.gc_id and i.gc_id:
        return c.gc_id == i.gc_id
    cd, idm = normalize_domain(c.gc_domain), normalize_domain(i.gc_domain)
    return bool(cd) and cd == idm


def evidence(c: Candidate, i: Incoming) -> Evidence:
    notes: list[str] = []
    gc_match = same_gc(c, i)

    # Hard keys
    if c.platform_ids & i.platform_ids:
        return Evidence(1.0, "platform_id", {}, ["same platform project id"], gc_match)
    if c.thread_ids & i.thread_ids:
        return Evidence(1.0, "thread", {}, ["same email thread"], gc_match)
    if (
        c.project_number
        and i.project_number
        and c.project_number.strip().lower() == i.project_number.strip().lower()
        and gc_match
    ):
        return Evidence(1.0, "project_number", {}, ["same project number and GC"], gc_match)

    comps: dict[str, float] = {}
    weights: dict[str, float] = {}
    raw_sim = name_similarity(c.project_name, i.project_name)
    sim = raw_sim
    weak_name = (
        min(
            len(normalize_name(c.project_name).split()), len(normalize_name(i.project_name).split())
        )
        < 2
    )
    if weak_name:
        sim = min(sim, 0.7)  # a one-token normalized name is weak evidence either way
        notes.append("project name is generic or very short")
    comps["name"], weights["name"] = sim, 0.45
    comps["gc"], weights["gc"] = (1.0 if gc_match else 0.0), 0.25

    geo_available = False
    geo = 0.0
    if c.lat is not None and c.lon is not None and i.lat is not None and i.lon is not None:
        km = _km(c.lat, c.lon, i.lat, i.lon)
        geo = 1.0 if km <= 1.0 else (0.5 if km <= 10 else 0.0)
        geo_available = True
        if km > 1.0 and raw_sim >= 0.85:
            notes.append(f"names match but sites are {km:.1f} km apart")
    elif c.city and i.city:
        geo = 1.0 if c.city.strip().lower() == i.city.strip().lower() else 0.0
        geo_available = True
    if geo_available:
        comps["geo"], weights["geo"] = geo, 0.12

    if c.bid_due and i.bid_due:
        delta_days = abs((c.bid_due - i.bid_due).total_seconds()) / 86400
        due = 1.0 if delta_days <= 3 else (0.5 if delta_days <= 14 else 0.0)
        if delta_days > 45:
            notes.append(f"due dates {delta_days:.0f} days apart (rebid or phase?)")
        comps["due"], weights["due"] = due, 0.12

    if c.owner_name and i.owner_name:
        owner = 1.0 if name_similarity(c.owner_name, i.owner_name) >= 0.85 else 0.0
        comps["owner"], weights["owner"] = owner, 0.06

    # Weighted evidence over the components we actually have. Name dominates; GC, geo, due corroborate.
    total_w = sum(weights.values())
    score = sum(weights[k] * comps[k] for k in weights) / total_w if total_w else 0.0
    # Distinct sites must not auto-merge.
    if geo_available and geo == 0.0:
        score = min(score, AUTO_MERGE - 0.01)
    # Same-name, same-GC but far-apart due dates: separate rebid.
    if c.bid_due and i.bid_due and abs((c.bid_due - i.bid_due).days) > 45 and i.kind == "itb":
        score = min(score, REVIEW - 0.01)
    return Evidence(round(score, 4), None, comps, notes, gc_match)


def decide(ev: Evidence, *, incoming_kind: str, candidate_same_gc: bool) -> str:
    """Returns one of: merge, review, new, related."""
    if (
        incoming_kind == "itb"
        and not candidate_same_gc
        and ev.hard_key is None
        and ev.score >= REVIEW
    ):
        return "related"  # different GC on the same project: never merge (SPEC-03 F2.4)
    if ev.score >= AUTO_MERGE:
        return "merge"
    if ev.score >= REVIEW:
        return "review"
    return "new"
