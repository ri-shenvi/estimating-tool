"""GC stats (SPEC-07 F4). Pure computation over opportunity summaries; persistence is the caller's job."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass
class OppSummary:
    first_seen_at: datetime
    bid_due: datetime | None
    ever_bidding: bool
    submitted: bool
    won: bool


@dataclass
class Stats:
    invites: int
    bids: int
    submitted: int
    won: int
    hit_rate: float | None
    avg_days_notice: float | None


def compute_stats(
    opps: list[OppSummary], *, now: datetime, window_days: int = 365, min_sample: int = 5
) -> Stats:
    cutoff = now - timedelta(days=window_days)
    recent = [o for o in opps if o.first_seen_at >= cutoff]
    invites = len(recent)
    bids = sum(1 for o in recent if o.ever_bidding)
    submitted = sum(1 for o in recent if o.submitted)
    won = sum(1 for o in recent if o.won)
    hit_rate = (won / submitted) if submitted >= min_sample else None
    notice = [(o.bid_due - o.first_seen_at).total_seconds() / 86400 for o in recent if o.bid_due]
    avg_notice = sum(notice) / len(notice) if notice else None
    return Stats(invites, bids, submitted, won, hit_rate, avg_notice)
