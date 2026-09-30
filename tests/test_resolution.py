from datetime import datetime, timedelta

from bidtriage.core.clock import BUSINESS_TZ
from bidtriage.resolution import Candidate, Incoming, decide, evidence, merge_date, normalize_name
from bidtriage.resolution.merge import addendum_gaps

DUE = datetime(2026, 10, 16, 14, tzinfo=BUSINESS_TZ)


def _cand(**kw):  # type: ignore[no-untyped-def]
    base = dict(
        opportunity_id="o1",
        project_name="Pitt - Benedum Hall Lab Renovation",
        gc_id="pjdick",
        gc_domain="pjdick.com",
        city="Pittsburgh",
        lat=40.4435,
        lon=-79.9580,
        bid_due=DUE,
        owner_name="University of Pittsburgh",
    )
    base.update(kw)
    return Candidate(**base)


def _inc(**kw):  # type: ignore[no-untyped-def]
    base = dict(
        project_name="Benedum Hall Lab Renovation",
        gc_id="pjdick",
        gc_domain="pjdick.com",
        city="Pittsburgh",
        lat=None,
        lon=None,
        bid_due=DUE,
        owner_name=None,
    )
    base.update(kw)
    return Incoming(**base)


def test_normalize_name():  # type: ignore[no-untyped-def]
    assert normalize_name("Bldg 3 Fitout") == normalize_name("Building Three Fit-Out")
    assert normalize_name("Pitt - Benedum Hall Lab Renovation Project") == "pitt benedum hall lab"


def test_platform_id_hard_key():  # type: ignore[no-untyped-def]
    ev = evidence(
        _cand(platform_ids={"abc123"}, project_name="Something Else"), _inc(platform_ids={"abc123"})
    )
    assert ev.score == 1.0 and ev.hard_key == "platform_id"


def test_thread_hard_key():  # type: ignore[no-untyped-def]
    ev = evidence(
        _cand(thread_ids={"<m1@x>"}), _inc(thread_ids={"<m1@x>"}, project_name="RE: whatever")
    )
    assert ev.hard_key == "thread"


def test_channel_switch_soft_merge():  # type: ignore[no-untyped-def]
    ev = evidence(_cand(), _inc())
    assert ev.score >= 0.9
    assert decide(ev, incoming_kind="itb", candidate_same_gc=ev.same_gc) == "merge"


def test_rename_lands_in_review():  # type: ignore[no-untyped-def]
    ev = evidence(
        _cand(
            project_name="Bldg 3 Fitout",
            city=None,
            lat=None,
            lon=None,
            bid_due=None,
            owner_name=None,
        ),
        _inc(
            project_name="Building Three Tenant Improvement - Electrical", city=None, bid_due=None
        ),
    )
    assert 0.6 <= ev.score < 0.9
    assert decide(ev, incoming_kind="addendum", candidate_same_gc=True) == "review"


def test_generic_name_distinct_sites_not_merged():  # type: ignore[no-untyped-def]
    ev = evidence(
        _cand(project_name="Office Renovation", lat=40.44, lon=-79.99),
        _inc(project_name="Office Renovation", lat=40.55, lon=-80.10),
    )
    assert ev.score < 0.9 and any("apart" in n for n in ev.notes)


def test_rebid_60_days_apart_is_new():  # type: ignore[no-untyped-def]
    ev = evidence(_cand(), _inc(bid_due=DUE + timedelta(days=60)))
    assert decide(ev, incoming_kind="itb", candidate_same_gc=True) == "new"


def test_different_gc_same_project_is_related():  # type: ignore[no-untyped-def]
    ev = evidence(
        _cand(),
        _inc(
            gc_id="mascaro",
            gc_domain="mascaroconstruction.com",
            project_name="Pitt - Benedum Hall Lab Renovation",
        ),
    )
    assert decide(ev, incoming_kind="itb", candidate_same_gc=ev.same_gc) == "related"


def test_addendum_unknown_due_still_merges():  # type: ignore[no-untyped-def]
    ev = evidence(
        _cand(), _inc(bid_due=None, project_name="Pitt Benedum Hall Lab Renovation - Addendum 2")
    )
    assert decide(ev, incoming_kind="addendum", candidate_same_gc=True) == "merge"


def test_merge_date_rules():  # type: ignore[no-untyped-def]
    new = DUE + timedelta(days=5)
    out = merge_date(DUE, new, incoming_kind="date_change")
    assert out.changed and out.value == new and "moved" in out.note
    out = merge_date(DUE, DUE + timedelta(days=1), incoming_kind="reminder")
    assert not out.changed and out.conflict and out.value == DUE
    out = merge_date(DUE, DUE.replace(hour=9), incoming_kind="reminder")
    assert not out.changed and not out.conflict
    out = merge_date(DUE, new, incoming_kind="date_change", locked=True)
    assert not out.changed and out.conflict and out.value == DUE
    out = merge_date(None, new, incoming_kind="reminder")
    assert out.changed and out.value == new
    out = merge_date(DUE, new, incoming_kind="rfi_response")
    assert not out.changed


def test_addendum_gaps():  # type: ignore[no-untyped-def]
    assert addendum_gaps([1, 3]) == [2]
    assert addendum_gaps([1, 2]) == []
    assert addendum_gaps([]) == []
