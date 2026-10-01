"""Soft-key evidence scoring for candidate opportunities (SPEC-03 F2)."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime

from rapidfuzz import fuzz

from bidtriage.resolution.normalize import normalize_domain, normalize_name

AUTO_MERGE = 0.9
REVIEW = 0.6

# A fresh solicitation, as opposed to an update to one. Only these carry the "never merge across
# GCs" and "far-apart dates mean a rebid" rules: an addendum with an odd date is still an addendum
# to the job we already have (SPEC-03 F2.4).
SOLICITATION_KINDS = {"itb", "rfb"}
# Beyond this, the same name and GC means a rebid or a later phase, not the same solicitation.
REBID_DAYS = 45

#: Below this name similarity, two records naming different projects cannot be attached to each
#: other however much else agrees. One GC bidding two jobs in one city inside a month is ordinary,
#: and without this floor the GC, city and a loose date window can outvote names that plainly
#: disagree — the false merge ADR-008 ranks as the worst outcome. Measured on the corpus, everything
#: that should match scores above 0.8 and the near misses below 0.35, so the floor sits in the gap.
NAME_FLOOR = 0.5


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
    # Raw name similarity before the generic-name penalty; how confident we are that these two
    # records name the same real-world project, which is what "related" hangs on.
    raw_name_similarity: float = 0.0
    # Set when the score was capped for a specific structural reason, so the caller can link the
    # two as related projects instead of silently creating an unconnected duplicate.
    rebid: bool = False
    distinct_site: bool = False
    # Both sides name a GC and they disagree — distinct from "we do not know the GC".
    gc_conflict: bool = False


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


def different_gc(c: Candidate, i: Incoming) -> bool:
    """True only when both sides name a GC and the two disagree.

    "We could not tell who the GC is" is not the same as "a different GC", and only the latter
    forbids a merge (SPEC-03 F2.4).
    """
    if c.gc_id and i.gc_id:
        return c.gc_id != i.gc_id
    cd, idm = normalize_domain(c.gc_domain), normalize_domain(i.gc_domain)
    return bool(cd) and bool(idm) and cd != idm


def evidence(c: Candidate, i: Incoming) -> Evidence:
    notes: list[str] = []
    gc_match = same_gc(c, i)
    gc_clash = different_gc(c, i)

    # Hard keys
    if c.platform_ids & i.platform_ids:
        return Evidence(
            1.0,
            "platform_id",
            {},
            ["same platform project id"],
            gc_match,
            1.0,
            gc_conflict=gc_clash,
        )
    if c.thread_ids & i.thread_ids:
        return Evidence(
            1.0, "thread", {}, ["same email thread"], gc_match, 1.0, gc_conflict=gc_clash
        )
    if (
        c.project_number
        and i.project_number
        and c.project_number.strip().lower() == i.project_number.strip().lower()
        and gc_match
    ):
        return Evidence(
            1.0,
            "project_number",
            {},
            ["same project number and GC"],
            gc_match,
            1.0,
            gc_conflict=gc_clash,
        )

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
    if gc_match or gc_clash:
        # Only score the GC when we actually know one on both sides. "We could not tell" is absence
        # of evidence, and scoring it zero would quietly split a project nobody named a GC for.
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

    far_apart = False
    if c.bid_due and i.bid_due:
        delta_days = abs((c.bid_due - i.bid_due).total_seconds()) / 86400
        due = 1.0 if delta_days <= 3 else (0.5 if delta_days <= 14 else 0.0)
        if i.kind not in SOLICITATION_KINDS:
            # An addendum or date change exists in order to move the date, so a date that
            # disagrees is not evidence against the match — but a date that agrees is still
            # evidence for it, and still separates two rebids of one project.
            due = max(due, 0.5)
        if delta_days > REBID_DAYS:
            far_apart = True
            notes.append(f"due dates {delta_days:.0f} days apart (rebid or phase?)")
        comps["due"], weights["due"] = due, 0.12

    if c.owner_name and i.owner_name:
        owner = 1.0 if name_similarity(c.owner_name, i.owner_name) >= 0.85 else 0.0
        comps["owner"], weights["owner"] = owner, 0.06

    # Weighted evidence over the components we actually have. Name dominates; GC, geo, due corroborate.
    total_w = sum(weights.values())
    score = sum(weights[k] * comps[k] for k in weights) / total_w if total_w else 0.0
    # Distinct sites must not auto-merge.
    # Two geocoded sites more than a city apart are two projects, whatever the names say. A
    # duplicate is recoverable; a false merge that hides one of two due dates is not (ADR-008).
    distinct_site = geo_available and geo == 0.0
    if distinct_site:
        score = min(score, REVIEW - 0.01)
    # Same-name, same-GC but far-apart due dates: separate rebid.
    rebid = far_apart and gc_match and raw_sim >= 0.85 and i.kind in SOLICITATION_KINDS
    if rebid:
        score = min(score, REVIEW - 0.01)
    # Names that disagree veto the match outright.
    if normalize_name(c.project_name) and normalize_name(i.project_name) and raw_sim < NAME_FLOOR:
        score = min(score, REVIEW - 0.01)
        notes.append(f"project names disagree (similarity {raw_sim:.2f})")
    return Evidence(
        round(score, 4),
        None,
        comps,
        notes,
        gc_match,
        raw_name_similarity=round(raw_sim, 4),
        rebid=rebid,
        distinct_site=distinct_site,
        gc_conflict=gc_clash,
    )


#: Above this raw name similarity, two records that must not merge are still worth linking so an
#: estimator sees the other one (SPEC-03 F2.4, and the rebid row of the edge-case table).
RELATED_NAME_SIMILARITY = 0.85


def decide(ev: Evidence, *, incoming_kind: str) -> str:
    """Returns one of: merge, review, new, related.

    `related` means "make a new opportunity, and link the two": the right answer whenever the
    names agree but a structural rule says these are different solicitations. Those rules are
    checked before the hard keys, because a false merge can hide a due date and a hard key is not
    proof: Ferry replying to all can put two GCs' invitations in one email thread.
    """
    if (
        incoming_kind in SOLICITATION_KINDS
        and ev.gc_conflict
        and ev.raw_name_similarity >= RELATED_NAME_SIMILARITY
    ):
        return "related"  # different GC on the same project: never merge (SPEC-03 F2.4)
    if ev.rebid:
        return "related"  # rebid or later phase of a job we know
    if ev.hard_key is not None:
        return "merge"
    if ev.score >= AUTO_MERGE:
        return "merge"
    if ev.score >= REVIEW:
        return "review"
    return "new"
