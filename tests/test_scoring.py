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


def test_inconsistent_size_signals(now):  # type: ignore[no-untyped-def]
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


def test_conflicting_labor_flags(now):  # type: ignore[no-untyped-def]
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


def test_freeze_after_submit(now):  # type: ignore[no-untyped-def]
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


def test_wv_distance(now):  # type: ignore[no-untyped-def]
    r = score(
        _opp(distance_miles=70, bid_due=now + timedelta(days=14)),
        GCSnapshot(tier="A"),
        CalendarSnapshot(now=now),
        DEFAULT_PROFILE,
    )
    d = next(c for c in r.contributions if c.factor == "distance")
    assert 0.4 < d.value < 0.6


def test_size_range_midpoint(now):  # type: ignore[no-untyped-def]
    """The prompt stores the midpoint of a stated range; the explanation has to say so (F2)."""
    o = _opp(
        stated_project_value=1_350_000,
        size_source="Electrical budget is in the $1.2 - 1.5 million range.",
        bid_due=now + timedelta(days=14),
    )
    r = score(o, GCSnapshot(tier="A"), CalendarSnapshot(now=now), DEFAULT_PROFILE)
    size = next(c for c in r.contributions if c.factor == "size")
    assert "$1.2 - 1.5 million" in size.reason and "midpoint used" in size.reason
    # The midpoint, not either end, is what was scored.
    assert r.size_estimate.value == pytest.approx(1_350_000 * 0.13)


def test_distance_unknown(now):  # type: ignore[no-untyped-def]
    r = score(
        _opp(distance_miles=None, bid_due=now + timedelta(days=14)),
        GCSnapshot(tier="A"),
        CalendarSnapshot(now=now),
        DEFAULT_PROFILE,
    )
    d = next(c for c in r.contributions if c.factor == "distance")
    assert d.value == 0.6 and "location" in r.missing_inputs
    # A geocode that failed is not an argument either way.
    assert "distance" not in [c.factor for c in r.top_positive(3) + r.top_negative(3)]


def test_timing_unknown(now):  # type: ignore[no-untyped-def]
    r = score(
        _opp(bid_due=None),
        GCSnapshot(tier="A"),
        CalendarSnapshot(now=now),
        DEFAULT_PROFILE,
    )
    t = next(c for c in r.contributions if c.factor == "timing")
    assert t.value == 0.5 and t.reason == "due date unknown"


def test_due_tomorrow(now):  # type: ignore[no-untyped-def]
    soon = score(
        _opp(bid_due=now + timedelta(days=1)),
        GCSnapshot(tier="A"),
        CalendarSnapshot(now=now),
        DEFAULT_PROFILE,
    )
    t = next(c for c in soon.contributions if c.factor == "timing")
    assert t.value == pytest.approx(0.2) and t.reason == "due in 1 day"
    assert [c.factor for c in soon.top_negative(1)] == ["timing"]
    roomy = score(
        _opp(bid_due=now + timedelta(days=14)),
        GCSnapshot(tier="A"),
        CalendarSnapshot(now=now),
        DEFAULT_PROFILE,
    )
    assert soon.score < roomy.score


def test_explanation_lists_are_disjoint(now):  # type: ignore[no-untyped-def]
    """F6: a factor at or below neutral is a reason for nothing, and never for both things."""
    blank = score(
        OpportunitySnapshot(project_type="unknown"),
        GCSnapshot(),
        CalendarSnapshot(now=now),
        DEFAULT_PROFILE,
    )
    assert blank.top_positive(3) == [] and blank.top_negative(1) == []

    # The shape that produced "size unknown (+) ... size unknown (-)" on every digest row.
    r = score(
        OpportunitySnapshot(project_type="government_civic", bid_due=now + timedelta(days=60)),
        GCSnapshot(),
        CalendarSnapshot(now=now),
        DEFAULT_PROFILE,
    )
    positive = {c.factor for c in r.top_positive(3)}
    negative = {c.factor for c in r.top_negative(3)}
    assert not (positive & negative)
    assert "size" not in positive and "gc" not in positive
    assert positive == {"project_type", "timing"}


def test_profile_missing_type(now):  # type: ignore[no-untyped-def]
    table = dict(DEFAULT_PROFILE.type_table)
    del table["healthcare"]
    p = Profile(type_table=table)
    assert any("healthcare" in w for w in p.validation_warnings())
    r = score(
        _opp(project_type="healthcare", bid_due=now + timedelta(days=14)),
        GCSnapshot(tier="A"),
        CalendarSnapshot(now=now),
        p,
    )
    pt = next(c for c in r.contributions if c.factor == "project_type")
    assert pt.value == p.type_table["other"]
    assert any("not in the profile" in w for w in r.warnings)


def test_past_due_reason_only_when_the_date_is_shaky(now):  # type: ignore[no-untyped-def]
    """F3: `past_due_at_receipt` is a prompt to verify, not a second cap."""
    shaky = score(
        _opp(
            bid_due=now - timedelta(days=1), flags=["past_due_at_receipt"], bid_due_confidence=0.3
        ),
        GCSnapshot(tier="A"),
        CalendarSnapshot(now=now),
        DEFAULT_PROFILE,
    )
    assert "Due date uncertain, verify" in shaky.warnings
    sure = score(
        _opp(
            bid_due=now - timedelta(days=1), flags=["past_due_at_receipt"], bid_due_confidence=0.9
        ),
        GCSnapshot(tier="A"),
        CalendarSnapshot(now=now),
        DEFAULT_PROFILE,
    )
    assert "Due date uncertain, verify" not in sure.warnings


def test_owner_direct_uses_owner_tier_else_default(now):  # type: ignore[no-untyped-def]
    """F1: a Separations Act prime is scored on the owner, not on a GC nobody named."""
    unlisted = score(
        _opp(gc_is_owner_direct=True, bid_due=now + timedelta(days=14)),
        GCSnapshot(),
        CalendarSnapshot(now=now),
        DEFAULT_PROFILE,
    )
    assert next(c for c in unlisted.contributions if c.factor == "gc").value == 0.6
    listed = score(
        _opp(gc_is_owner_direct=True, bid_due=now + timedelta(days=14)),
        GCSnapshot(tier="A"),
        CalendarSnapshot(now=now),
        DEFAULT_PROFILE,
    )
    assert next(c for c in listed.contributions if c.factor == "gc").value == 1.0


def test_default_type_table_covers_every_extracted_type():  # type: ignore[no-untyped-def]
    """A type the model can emit but the profile never mentions would score as `other` in silence."""
    from bidtriage.extraction.schema import ProjectType

    missing = {t.value for t in ProjectType} - set(DEFAULT_PROFILE.type_table)
    assert not missing
    assert not DEFAULT_PROFILE.validation_warnings()


# ------------------------------------------------------- calibration harness (ADR-005)


def _labeled(n, *, band, label, project_type="higher_education", gc_tier="A"):  # type: ignore[no-untyped-def]
    from bidtriage.scoring.calibrate import LabeledScore

    return [
        LabeledScore(
            opportunity_id=f"o{i}",
            label=label,
            band=band,
            score=80 if band == "bid" else 30,
            project_type=project_type,
            gc_tier=gc_tier,
        )
        for i in range(n)
    ]


def test_calibration_report_scores_the_bid_band():  # type: ignore[no-untyped-def]
    from bidtriage.scoring.calibrate import confusion, report

    rows = (
        _labeled(9, band="bid", label="bid")
        + _labeled(1, band="bid", label="pass")
        + _labeled(1, band="consider", label="bid", project_type="k12_education", gc_tier="C")
        + _labeled(9, band="pass", label="pass", project_type="retail_restaurant", gc_tier="D")
    )
    c = confusion(rows)
    assert (c.tp, c.fp, c.fn, c.tn) == (9, 1, 1, 9)
    assert c.precision == pytest.approx(0.9) and c.recall == pytest.approx(0.9)

    text, passed = report(rows)
    assert passed
    assert "By project type" in text and "By GC tier" in text
    assert "higher_education" in text and "tier D" in text


def test_calibration_report_fails_below_goal():  # type: ignore[no-untyped-def]
    from bidtriage.scoring.calibrate import report

    rows = _labeled(5, band="bid", label="bid") + _labeled(5, band="bid", label="pass")
    text, passed = report(rows)
    assert not passed and "BELOW GOAL" in text

    empty, passed_empty = report([])
    assert not passed_empty and "nothing to calibrate" in empty
