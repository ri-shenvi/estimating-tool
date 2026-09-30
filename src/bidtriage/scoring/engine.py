"""Pure scoring function (SPEC-04). Same inputs + same profile => same output, byte for byte."""

from __future__ import annotations

import hashlib
import json
from datetime import timedelta

from pydantic import BaseModel, Field

from bidtriage.scoring.profile import Profile
from bidtriage.scoring.snapshot import CalendarSnapshot, GCSnapshot, OpportunitySnapshot

RENEWABLE_SCOPES = {"solar_pv", "battery_storage", "ev_charging"}
DB_SCOPES = {"design_build_engineering", "bim_coordination"}


class Contribution(BaseModel):
    factor: str
    weight: float
    value: float
    contribution: float
    reason: str


class Adjustment(BaseModel):
    name: str
    amount: int
    reason: str


class SizeEstimate(BaseModel):
    value: float | None
    method: str
    reason: str


class ScoreResult(BaseModel):
    score: int
    band: str
    raw_weighted: float
    contributions: list[Contribution]
    caps: list[Adjustment] = Field(default_factory=list)
    boosts: list[Adjustment] = Field(default_factory=list)
    missing_inputs: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    size_estimate: SizeEstimate
    inputs_hash: str

    def top_positive(self, n: int = 3) -> list[Contribution]:
        return sorted(self.contributions, key=lambda c: -c.contribution)[:n]

    def top_negative(self, n: int = 1) -> list[Contribution]:
        ranked = sorted(self.contributions, key=lambda c: c.contribution - c.weight * 100)
        return [c for c in ranked[:n] if c.value < 0.7]


def estimate_electrical_value(o: OpportunitySnapshot, p: Profile) -> SizeEstimate:
    share = p.electrical_share.get(o.project_type, p.electrical_share["unknown"])
    if o.stated_electrical_value:
        reason = f"stated electrical value ${o.stated_electrical_value:,.0f}"
        if o.stated_project_value and o.stated_electrical_value / o.stated_project_value > 0.5:
            reason += " (electrical share unusually high, verify)"
        return SizeEstimate(
            value=o.stated_electrical_value, method="stated_electrical", reason=reason
        )
    if o.stated_project_value:
        v = o.stated_project_value * share
        return SizeEstimate(
            value=v,
            method="project_value_x_share",
            reason=f"~${v:,.0f} electrical, from ${o.stated_project_value:,.0f} project value × {share:.0%} {o.project_type.replace('_', ' ')} share",
        )
    if o.square_feet:
        cost = p.cost_per_sf.get(o.project_type, p.cost_per_sf["unknown"])
        v = o.square_feet * cost * share
        return SizeEstimate(
            value=v,
            method="sf_x_cost_x_share",
            reason=f"~${v:,.0f} electrical, from {o.square_feet:,.0f} SF × ${cost:,.0f}/SF × {share:.0%} share",
        )
    return SizeEstimate(value=None, method="unknown", reason="size unknown")


def _size_factor(value: float | None, p: Profile) -> float:
    b = p.size_band
    if value is None:
        return b.unknown_value
    if value < b.min_floor:
        return 0.0
    if value < b.sweet_low:
        return (value - b.min_floor) / (b.sweet_low - b.min_floor)
    if value <= b.sweet_high:
        return 1.0
    if value <= b.max_ceiling:
        return 1.0 - (1.0 - b.ceiling_value) * (value - b.sweet_high) / (
            b.max_ceiling - b.sweet_high
        )
    if value <= b.hard_max:
        return b.ceiling_value * (1 - (value - b.max_ceiling) / (b.hard_max - b.max_ceiling))
    return 0.0


def _distance_factor(miles: float | None, p: Profile) -> float:
    d = p.distance
    if miles is None:
        return d.unknown_value
    if miles <= d.near_miles:
        return d.near_value
    if miles <= d.mid_miles:
        return d.near_value + (d.mid_value - d.near_value) * (miles - d.near_miles) / (
            d.mid_miles - d.near_miles
        )
    if miles <= d.far_miles:
        return d.mid_value + (d.far_value - d.mid_value) * (miles - d.mid_miles) / (
            d.far_miles - d.mid_miles
        )
    return d.beyond_value


def _timing_factor(o: OpportunitySnapshot, cal: CalendarSnapshot, p: Profile) -> tuple[float, str]:
    t = p.timing
    if o.bid_due is None:
        base, why = t.unknown, "due date unknown"
    else:
        days = (o.bid_due - cal.now).total_seconds() / 86400
        if days < 0:
            base, why = 0.0, "due date has passed"
        elif days < 3:
            base, why = t.under_3_days, f"due in {max(int(days), 0)} day(s)"
        elif days < 7:
            base, why = t.days_3_to_6, f"due in {int(days)} days"
        elif days <= 21:
            base, why = t.days_7_to_21, f"due in {int(days)} days"
        elif days <= 45:
            base, why = t.days_22_to_45, f"due in {int(days)} days"
        else:
            base, why = t.over_45, f"due in {int(days)} days"
    n = cal.other_bids_due_same_week
    if n >= 3:
        base *= t.congestion_3_plus
        why += f"; {n} other bids due that week"
    elif n == 2:
        base *= t.congestion_2
        why += "; 2 other bids due that week"
    return base, why


def _gc_factor(gc: GCSnapshot, o: OpportunitySnapshot, p: Profile) -> tuple[float, str]:
    if o.gc_is_owner_direct and gc.tier == "unknown":
        return p.owner_direct_default, "owner-direct solicitation"
    base = p.gc_tier.get(gc.tier, p.gc_tier["unknown"])
    why = f"GC tier {gc.tier}" if gc.tier != "unknown" else "GC tier not set"
    if gc.hit_rate_12m is not None and gc.submitted_12m >= p.hit_rate_min_sample:
        if gc.hit_rate_12m >= p.hit_rate_high:
            base = min(1.0, base + p.hit_rate_adjust)
            why += f"; hit rate {gc.hit_rate_12m:.0%} (+)"
        elif gc.hit_rate_12m < p.hit_rate_low:
            base = max(0.0, base - p.hit_rate_adjust)
            why += f"; hit rate {gc.hit_rate_12m:.0%} (−)"
    return base, why


def _bid_type_factor(o: OpportunitySnapshot, p: Profile) -> tuple[float, str]:
    key = o.bid_type
    if key == "hard_bid":
        key = "hard_bid_public" if o.sector in ("public", "federal") else "hard_bid_private"
    return p.bid_type.get(key, p.bid_type["unknown"]), key.replace("_", " ")


def band_for(score: int, p: Profile) -> str:
    if score >= p.thresholds.bid:
        return "bid"
    if score >= p.thresholds.consider:
        return "consider"
    if score >= p.thresholds.likely_pass:
        return "likely_pass"
    return "pass"


def score(o: OpportunitySnapshot, gc: GCSnapshot, cal: CalendarSnapshot, p: Profile) -> ScoreResult:
    w = p.weights
    contributions: list[Contribution] = []
    missing: list[str] = []
    warnings: list[str] = []
    flags = set(o.flags)

    # project type
    tv = p.type_table.get(o.project_type, p.type_table.get("other", 0.4))
    reason = o.project_type.replace("_", " ")
    if o.trade_relevance == "partial":
        tv *= p.partial_relevance_multiplier
        reason += " (electrical is a minor part)"
    elif o.trade_relevance == "none":
        tv = 0.0
        reason = "no electrical scope"
    if o.project_type == "unknown":
        missing.append("project_type")
    contributions.append(
        Contribution(
            factor="project_type",
            weight=w.project_type,
            value=tv,
            contribution=w.project_type * tv * 100,
            reason=reason,
        )
    )

    # size
    est = estimate_electrical_value(o, p)
    sv = _size_factor(est.value, p)
    if est.value is None:
        missing.append("size")
    if "unusually high" in est.reason:
        warnings.append("electrical share unusually high, verify")
    contributions.append(
        Contribution(
            factor="size",
            weight=w.size,
            value=sv,
            contribution=w.size * sv * 100,
            reason=est.reason,
        )
    )

    # gc
    gv, greason = _gc_factor(gc, o, p)
    if gc.tier == "unknown" and not o.gc_is_owner_direct:
        missing.append("gc_tier")
    contributions.append(
        Contribution(
            factor="gc", weight=w.gc, value=gv, contribution=w.gc * gv * 100, reason=greason
        )
    )

    # distance
    dv = _distance_factor(o.distance_miles, p)
    if o.distance_miles is None:
        missing.append("location")
        dreason = "distance unknown"
    else:
        dreason = f"{o.distance_miles:.0f} miles from shop"
    contributions.append(
        Contribution(
            factor="distance",
            weight=w.distance,
            value=dv,
            contribution=w.distance * dv * 100,
            reason=dreason,
        )
    )

    # timing
    tmv, treason = _timing_factor(o, cal, p)
    contributions.append(
        Contribution(
            factor="timing",
            weight=w.timing,
            value=tmv,
            contribution=w.timing * tmv * 100,
            reason=treason,
        )
    )

    # bid type
    bv, breason = _bid_type_factor(o, p)
    contributions.append(
        Contribution(
            factor="bid_type",
            weight=w.bid_type,
            value=bv,
            contribution=w.bid_type * bv * 100,
            reason=breason,
        )
    )

    raw = sum(c.contribution for c in contributions)
    total = raw

    # boosts
    boosts: list[Adjustment] = []
    if RENEWABLE_SCOPES & set(o.scope_items):
        boosts.append(
            Adjustment(
                name="renewables", amount=p.boosts.renewables, reason="solar / storage / EV scope"
            )
        )
    owner_key = o.owner_name and any(k.lower() in o.owner_name.lower() for k in p.key_accounts)
    if gc.key_account or owner_key:
        boosts.append(
            Adjustment(name="key_account", amount=p.boosts.key_account, reason="key account")
        )
    if DB_SCOPES & set(o.scope_items):
        boosts.append(
            Adjustment(
                name="design_build_or_bim",
                amount=p.boosts.design_build_or_bim,
                reason="design-build or BIM scope",
            )
        )
    if "sustainability" in flags:
        boosts.append(
            Adjustment(
                name="sustainability",
                amount=p.boosts.sustainability,
                reason="sustainability goals stated",
            )
        )
    if "requested_by_name" in flags:
        boosts.append(
            Adjustment(
                name="requested_by_name",
                amount=p.boosts.requested_by_name,
                reason="GC asked for Ferry by name",
            )
        )
    total += sum(b.amount for b in boosts)

    # caps and hard filters
    caps: list[Adjustment] = []

    def cap(name: str, limit: int, why: str) -> None:
        nonlocal total
        if total > limit:
            caps.append(Adjustment(name=name, amount=limit, reason=why))
            total = float(limit)

    if gc.tier == "blocked":
        caps.append(Adjustment(name="gc_blocked", amount=0, reason="GC is on the do-not-bid list"))
        total = 0.0
    if o.trade_relevance == "none":
        cap("no_electrical_scope", p.caps.no_electrical_scope, "No electrical scope")
    if "open_shop_indicated" in flags:
        if "union_required" in flags or "pla" in flags:
            warnings.append("conflicting labor flags, verify")
        else:
            cap("open_shop", p.caps.open_shop, "Open-shop pricing indicated")
    if (
        o.prebid_mandatory
        and o.prebid_at is not None
        and o.prebid_at < cal.now
        and o.status not in ("bidding", "submitted")
    ):
        cap(
            "mandatory_prebid_passed",
            p.caps.mandatory_prebid_passed,
            f"Mandatory pre-bid on {o.prebid_at.date().isoformat()} already passed",
        )
    if (
        o.bid_due is not None
        and o.bid_due < cal.now
        and o.status not in ("submitted", "won", "lost")
    ):
        cap("due_passed", p.caps.due_passed, "Bid due date has passed")
    if est.value is not None and est.value > p.size_band.hard_max:
        cap("above_hard_max", p.caps.above_hard_max, "Above maximum size")
    if o.project_type == "residential_single_family":
        cap("residential", p.caps.residential, "Residential")
    if "past_due_at_receipt" in flags:
        warnings.append("Due date uncertain, verify")

    final = int(round(max(0.0, min(100.0, total))))
    inputs_hash = hashlib.sha256(
        json.dumps(
            {
                "o": o.model_dump(mode="json"),
                "gc": gc.model_dump(mode="json"),
                "cal": cal.model_dump(mode="json"),
                "p": p.model_dump(mode="json"),
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()
    return ScoreResult(
        score=final,
        band=band_for(final, p),
        raw_weighted=round(raw, 3),
        contributions=contributions,
        caps=caps,
        boosts=boosts,
        missing_inputs=missing,
        warnings=warnings,
        size_estimate=est,
        inputs_hash=inputs_hash,
    )


def days_until(dt, now) -> int | None:
    if dt is None:
        return None
    return int((dt - now) // timedelta(days=1))
