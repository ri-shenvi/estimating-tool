from datetime import timedelta

import pytest

from bidtriage.scoring import (
    DEFAULT_PROFILE,
    CalendarSnapshot,
    GCSnapshot,
    OpportunitySnapshot,
    Profile,
    score,
)
from bidtriage.scoring.engine import _size_factor, estimate_electrical_value


def _opp(**kw):  # type: ignore[no-untyped-def]
    base = dict(
        project_type="higher_education",
        stated_project_value=12_000_000,
        distance_miles=18,
        bid_type="hard_bid",
        sector="private",
    )
    base.update(kw)
    return OpportunitySnapshot(**base)


def test_default_bid_band(now):  # type: ignore[no-untyped-def]
    o = _opp(bid_due=now + timedelta(days=14))
    r = score(o, GCSnapshot(tier="A"), CalendarSnapshot(now=now), DEFAULT_PROFILE)
    assert r.band == "bid" and r.score >= 70
    top = [c.factor for c in r.top_positive(3)]
    assert set(top) == {"project_type", "size", "gc"}
    assert "1,560,000" in r.size_estimate.reason


def test_blocked_gc_zero(now):  # type: ignore[no-untyped-def]
    r = score(
        _opp(bid_due=now + timedelta(days=14)),
        GCSnapshot(tier="blocked"),
        CalendarSnapshot(now=now),
        DEFAULT_PROFILE,
    )
    assert r.score == 0 and any("do-not-bid" in c.reason for c in r.caps)


def test_no_electrical_scope_cap(now):  # type: ignore[no-untyped-def]
    r = score(
        _opp(trade_relevance="none", bid_due=now + timedelta(days=14)),
        GCSnapshot(tier="A"),
        CalendarSnapshot(now=now),
        DEFAULT_PROFILE,
    )
    assert r.score <= 5


def test_mandatory_prebid_passed_cap(now):  # type: ignore[no-untyped-def]
    r = score(
        _opp(
            bid_due=now + timedelta(days=14),
            prebid_at=now - timedelta(days=1),
            prebid_mandatory=True,
        ),
        GCSnapshot(tier="A"),
        CalendarSnapshot(now=now),
        DEFAULT_PROFILE,
    )
    assert r.score <= 10 and any("pre-bid" in c.reason.lower() for c in r.caps)


def test_unknown_size_neutral(now):  # type: ignore[no-untyped-def]
    r = score(
        _opp(stated_project_value=None, bid_due=now + timedelta(days=14)),
        GCSnapshot(tier="A"),
        CalendarSnapshot(now=now),
        DEFAULT_PROFILE,
    )
    size = next(c for c in r.contributions if c.factor == "size")
    assert size.value == 0.5 and "size" in r.missing_inputs


def test_congestion_multiplier(now):  # type: ignore[no-untyped-def]
    o = _opp(bid_due=now + timedelta(days=14))
    a = score(
        o,
        GCSnapshot(tier="A"),
        CalendarSnapshot(now=now, other_bids_due_same_week=0),
        DEFAULT_PROFILE,
    )
    b = score(
        o,
        GCSnapshot(tier="A"),
        CalendarSnapshot(now=now, other_bids_due_same_week=3),
        DEFAULT_PROFILE,
    )
    ta = next(c for c in a.contributions if c.factor == "timing")
    tb = next(c for c in b.contributions if c.factor == "timing")
    assert tb.value == pytest.approx(ta.value * 0.6) and "3 other bids" in tb.reason


def test_deterministic(now):  # type: ignore[no-untyped-def]
    o = _opp(bid_due=now + timedelta(days=14))
    a = score(o, GCSnapshot(tier="B"), CalendarSnapshot(now=now), DEFAULT_PROFILE)
    b = score(o, GCSnapshot(tier="B"), CalendarSnapshot(now=now), DEFAULT_PROFILE)
    assert a.model_dump() == b.model_dump()


def test_size_boundaries():  # type: ignore[no-untyped-def]
    p = DEFAULT_PROFILE
    assert _size_factor(250_000, p) == 1.0
    assert _size_factor(4_000_000, p) == 1.0
    assert _size_factor(8_000_000, p) == pytest.approx(0.2)
    assert _size_factor(15_000_000, p) == pytest.approx(0.0)
    assert _size_factor(15_000_001, p) == 0.0
    assert _size_factor(74_999, p) == 0.0


def test_size_range_and_inconsistent(now):  # type: ignore[no-untyped-def]
    est = estimate_electrical_value(
        _opp(stated_electrical_value=5_000_000, stated_project_value=6_000_000), DEFAULT_PROFILE
    )
    assert "unusually high" in est.reason
    r = score(
        _opp(
            stated_electrical_value=5_000_000,
            stated_project_value=6_000_000,
            bid_due=now + timedelta(days=14),
        ),
        GCSnapshot(tier="A"),
        CalendarSnapshot(now=now),
        DEFAULT_PROFILE,
    )
    assert any("unusually high" in w for w in r.warnings)


def test_clamp_100(now):  # type: ignore[no-untyped-def]
    r = score(
        _opp(
            bid_due=now + timedelta(days=14),
            scope_items=["solar_pv", "bim_coordination"],
            flags=["sustainability", "requested_by_name"],
        ),
        GCSnapshot(tier="A", key_account=True),
        CalendarSnapshot(now=now),
        DEFAULT_PROFILE,
    )
    assert r.score == 100


def test_conflicting_labor_flags_no_cap(now):  # type: ignore[no-untyped-def]
    r = score(
        _opp(bid_due=now + timedelta(days=14), flags=["open_shop_indicated", "union_required"]),
        GCSnapshot(tier="A"),
        CalendarSnapshot(now=now),
        DEFAULT_PROFILE,
    )
    assert r.score > 20 and any("conflicting labor" in w for w in r.warnings)


def test_open_shop_cap(now):  # type: ignore[no-untyped-def]
    r = score(
        _opp(bid_due=now + timedelta(days=14), flags=["open_shop_indicated"]),
        GCSnapshot(tier="A"),
        CalendarSnapshot(now=now),
        DEFAULT_PROFILE,
    )
    assert r.score <= 20


def test_hit_rate_min_sample(now):  # type: ignore[no-untyped-def]
    o = _opp(bid_due=now + timedelta(days=14))
    few = score(
        o,
        GCSnapshot(tier="B", submitted_12m=4, hit_rate_12m=0.75),
        CalendarSnapshot(now=now),
        DEFAULT_PROFILE,
    )
    enough = score(
        o,
        GCSnapshot(tier="B", submitted_12m=6, hit_rate_12m=0.33),
        CalendarSnapshot(now=now),
        DEFAULT_PROFILE,
    )
    gf = next(c for c in few.contributions if c.factor == "gc").value
    ge = next(c for c in enough.contributions if c.factor == "gc").value
    assert gf == 0.8 and ge == pytest.approx(0.9)


def test_due_passed_cap_but_frozen_after_submit(now):  # type: ignore[no-untyped-def]
    past = _opp(bid_due=now - timedelta(days=1))
    r = score(past, GCSnapshot(tier="A"), CalendarSnapshot(now=now), DEFAULT_PROFILE)
    assert r.score <= 5
    r2 = score(
        _opp(bid_due=now - timedelta(days=1), status="submitted"),
        GCSnapshot(tier="A"),
        CalendarSnapshot(now=now),
        DEFAULT_PROFILE,
    )
    assert r2.score > 5


def test_profile_validation():  # type: ignore[no-untyped-def]
    with pytest.raises(ValueError):
        Profile(
            weights={
                "project_type": 0.3,
                "size": 0.25,
                "gc": 0.25,
                "distance": 0.10,
                "timing": 0.10,
                "bid_type": 0.05,
            }
        )  # sums to 1.05
    with pytest.raises(ValueError):
        Profile(type_table={"other": 0.5})
    with pytest.raises(ValueError):
        Profile(
            size_band={
                "min_floor": 100,
                "sweet_low": 50,
                "sweet_high": 200,
                "max_ceiling": 300,
                "hard_max": 400,
            }
        )


def test_wv_distance_no_state_penalty(now):  # type: ignore[no-untyped-def]
    r = score(
        _opp(distance_miles=70, bid_due=now + timedelta(days=14)),
        GCSnapshot(tier="A"),
        CalendarSnapshot(now=now),
        DEFAULT_PROFILE,
    )
    d = next(c for c in r.contributions if c.factor == "distance")
    assert 0.4 < d.value < 0.6
