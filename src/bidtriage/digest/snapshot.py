"""Digest snapshot: the API contract consumed by HTML, text and (P1) Teams renderers."""

from __future__ import annotations

from datetime import datetime, timedelta

from pydantic import BaseModel, Field

BAND_ORDER = ["bid", "consider", "likely_pass", "pass"]
CONSIDER_CAP = 10
DECISION_WINDOW_DAYS = 10
STALE_DECISION_DAYS = 3


class DigestItem(BaseModel):
    opportunity_id: str
    project_name: str
    gc_name: str | None = None
    gc_tier: str = "unknown"
    project_type: str = "unknown"
    new_or_renovation: str = "unknown"
    location_short: str | None = None
    distance_miles: float | None = None
    size_reason: str = "size unknown"
    bid_due: datetime | None = None
    bid_due_time_known: bool = False
    prebid_at: datetime | None = None
    prebid_mandatory: bool | None = None
    rfi_deadline: datetime | None = None
    summary: str = ""
    exclusions: str | None = None
    addenda_count: int = 0
    addendum_gap: bool = False
    docs_host: str | None = None
    score: int = 0
    band: str = "pass"
    why_positive: list[str] = Field(default_factory=list)
    why_negative: list[str] = Field(default_factory=list)
    flags: list[str] = Field(default_factory=list)
    status: str = "new"
    assignee_id: str | None = None
    assignee_name: str | None = None
    first_seen_at: datetime | None = None
    changed_since_digest: bool = False
    change_summary: str | None = None
    is_new: bool = False
    actions: dict[str, str] = Field(default_factory=dict)
    open_url: str = ""


class ReviewItem(BaseModel):
    kind: str
    title: str
    url: str


class HealthLine(BaseModel):
    name: str
    status: str  # ok | degraded | down | paused
    detail: str = ""


class DigestSnapshot(BaseModel):
    date: str
    recipient_id: str
    recipient_name: str
    recipient_role: str = "estimator"
    timezone: str = "America/New_York"
    generated_at: datetime
    counts: dict[str, int] = Field(default_factory=dict)
    prebid_today: list[DigestItem] = Field(default_factory=list)
    needs_decision: list[DigestItem] = Field(default_factory=list)
    new_by_band: dict[str, list[DigestItem]] = Field(default_factory=dict)
    consider_overflow: int = 0
    hidden_low_count: int = 0
    due_this_week: list[DigestItem] = Field(default_factory=list)
    changed: list[DigestItem] = Field(default_factory=list)
    passed_changed: list[DigestItem] = Field(default_factory=list)
    needs_review: list[ReviewItem] = Field(default_factory=list)
    health: list[HealthLine] = Field(default_factory=list)
    degraded: bool = False
    quiet: bool = False
    in_progress_count: int = 0
    next_due: DigestItem | None = None
    review_url: str = ""
    calendar_url: str = ""
    profile_version: int = 0
    fallback: bool = False


def _sort_due(items: list[DigestItem]) -> list[DigestItem]:
    return sorted(
        items,
        key=lambda i: (
            i.bid_due is None,
            i.bid_due or datetime.max.replace(tzinfo=None).astimezone(),
            -i.score,
        ),
    )


def assemble(
    *,
    items: list[DigestItem],
    recipient_id: str,
    recipient_name: str,
    recipient_role: str,
    now: datetime,
    since: datetime | None,
    min_band: str = "pass",
    review: list[ReviewItem] | None = None,
    health: list[HealthLine] | None = None,
    review_url: str = "",
    calendar_url: str = "",
    profile_version: int = 0,
) -> DigestSnapshot:
    """Pure assembly of sections from item rows (SPEC-05 F3). No I/O."""
    today = now.date()
    week_end = now + timedelta(days=7)
    is_chief = recipient_role in ("chief", "admin")
    review = review or []
    health = health or []
    visible_bands = BAND_ORDER[: BAND_ORDER.index(min_band) + 1]

    for it in items:
        it.is_new = since is None or (it.first_seen_at is not None and it.first_seen_at > since)

    def mine(it: DigestItem) -> bool:
        return it.assignee_id == recipient_id or (is_chief and it.assignee_id is None)

    open_statuses = {"new", "undecided"}
    needs_decision = [
        it
        for it in items
        if it.status in open_statuses
        and mine(it)
        and (
            (it.bid_due is not None and it.bid_due <= now + timedelta(days=DECISION_WINDOW_DAYS))
            or (
                it.first_seen_at is not None
                and it.first_seen_at <= now - timedelta(days=STALE_DECISION_DAYS)
            )
        )
    ]
    needs_ids = {it.opportunity_id for it in needs_decision}

    new_items = [
        it
        for it in items
        if it.is_new and it.opportunity_id not in needs_ids and it.status in open_statuses
    ]
    new_by_band: dict[str, list[DigestItem]] = {b: [] for b in BAND_ORDER}
    for it in sorted(new_items, key=lambda i: (-i.score, i.bid_due is None, i.bid_due or now)):
        new_by_band[it.band].append(it)
    consider_overflow = max(0, len(new_by_band["consider"]) - CONSIDER_CAP)
    new_by_band["consider"] = new_by_band["consider"][:CONSIDER_CAP]
    hidden_low = 0
    for b in BAND_ORDER:
        if b not in visible_bands:
            hidden_low += len(new_by_band[b])
            new_by_band[b] = []

    due_this_week = _sort_due(
        [
            it
            for it in items
            if it.status in ("bidding", "undecided")
            and (
                (it.bid_due and now <= it.bid_due <= week_end)
                or (it.prebid_at and now <= it.prebid_at <= week_end)
            )
        ]
    )
    changed = [
        it
        for it in items
        if it.changed_since_digest and it.status in ("bidding", "undecided", "new", "snoozed")
    ]
    passed_changed = [it for it in items if it.changed_since_digest and it.status == "passed"]
    prebid_today = [
        it
        for it in items
        if it.prebid_at
        and it.prebid_at.astimezone(now.tzinfo).date() == today
        and it.status in ("bidding", "undecided", "new")
    ]

    degraded = any(h.status in ("degraded", "down") for h in health)
    in_progress = [it for it in items if it.status == "bidding"]
    next_due = _sort_due([it for it in in_progress if it.bid_due and it.bid_due >= now])
    counts = {
        "new": sum(len(v) for v in new_by_band.values()) + consider_overflow + hidden_low,
        "new_bid": len(new_by_band["bid"]),
        "due_this_week": len(due_this_week),
        "changed": len(changed),
        "needs_decision": len(needs_decision),
        "needs_review": len(review),
    }
    quiet = (
        not any(
            [
                needs_decision,
                new_items,
                due_this_week,
                changed,
                passed_changed,
                review,
                prebid_today,
            ]
        )
        and not degraded
    )
    return DigestSnapshot(
        date=today.isoformat(),
        recipient_id=recipient_id,
        recipient_name=recipient_name,
        recipient_role=recipient_role,
        timezone=str(now.tzinfo),
        generated_at=now,
        counts=counts,
        prebid_today=prebid_today,
        needs_decision=_sort_due(needs_decision),
        new_by_band=new_by_band,
        consider_overflow=consider_overflow,
        hidden_low_count=hidden_low,
        due_this_week=due_this_week,
        changed=changed,
        passed_changed=passed_changed,
        needs_review=review,
        health=health,
        degraded=degraded,
        quiet=quiet,
        in_progress_count=len(in_progress),
        next_due=next_due[0] if next_due else None,
        review_url=review_url,
        calendar_url=calendar_url,
        profile_version=profile_version,
    )
