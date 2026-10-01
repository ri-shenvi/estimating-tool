"""Unit tests for the pure matching and merge rules (SPEC-03 F2, F3).

The database-level behaviour these feed — sources, addenda, history, status — is in
`test_resolution_pipeline.py`.
"""

from datetime import datetime, timedelta

from bidtriage.core.clock import BUSINESS_TZ
from bidtriage.resolution import (
    Candidate,
    Incoming,
    addendum_gaps,
    award_outcome,
    decide,
    evidence,
    gap_detection_enabled,
    geohash,
    material_change,
    merge_date,
    merge_scope,
    normalize_name,
    size_change_ratio,
    with_change_count,
)

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
    assert decide(ev, incoming_kind="addendum") == "merge"


def test_channel_switch():
    """BuildingConnected invite then an addendum from a personal email: GC + name + date window."""
    ev = evidence(_cand(), _inc())
    assert ev.score >= 0.9
    assert decide(ev, incoming_kind="addendum") == "merge"


def test_rename_soft_match():
    """Renamed between ITB and addendum, with nothing else to go on: possible duplicates."""
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
    assert decide(ev, incoming_kind="addendum") == "review"


def test_rename_with_thread_merges_outright():
    """The same rename, but the addendum is a reply: the thread settles it (SPEC-03 edge cases)."""
    ev = evidence(
        _cand(project_name="Bldg 3 Fitout", thread_ids={"<itb-1@pjdick.com>"}),
        _inc(project_name="Building Three Tenant Improvement", thread_ids={"<itb-1@pjdick.com>"}),
    )
    assert decide(ev, incoming_kind="addendum") == "merge"


def test_generic_name_distinct_sites():
    """ "Office Renovation" twice from one GC at addresses > 1 km apart stays two opportunities."""
    ev = evidence(
        _cand(project_name="Office Renovation", lat=40.44, lon=-79.99),
        _inc(project_name="Office Renovation", lat=40.55, lon=-80.10),
    )
    assert ev.score < 0.9 and ev.distinct_site
    assert any("apart" in n for n in ev.notes)
    assert decide(ev, incoming_kind="itb") == "new"


def test_rebid_far_apart_dates_is_related():
    """Evidence half of the rebid case; the linking half is `test_rebid_not_merged`."""
    ev = evidence(_cand(), _inc(bid_due=DUE + timedelta(days=60)))
    assert ev.rebid and ev.score < 0.6
    assert decide(ev, incoming_kind="itb") == "related"
    # An addendum with a far-off date is still an addendum to the job we have.
    later = evidence(_cand(), _inc(bid_due=DUE + timedelta(days=60), kind="addendum"))
    assert not later.rebid and decide(later, incoming_kind="addendum") == "merge"


def test_different_gc_same_project_is_related():  # type: ignore[no-untyped-def]
    ev = evidence(
        _cand(),
        _inc(
            gc_id="mascaro",
            gc_domain="mascaroconstruction.com",
            project_name="Pitt - Benedum Hall Lab Renovation",
        ),
    )
    assert ev.gc_conflict
    assert decide(ev, incoming_kind="itb") == "related"


def test_unknown_gc_is_not_a_different_gc():
    """ "We could not tell who the GC is" must not read as "a different GC" (SPEC-03 F2.4)."""
    ev = evidence(
        _cand(gc_id=None, gc_domain=None),
        _inc(gc_id=None, gc_domain=None, lat=40.4435, lon=-79.958),
    )
    assert not ev.gc_conflict
    assert decide(ev, incoming_kind="itb") == "merge"


def test_addendum_unknown_due_still_merges():  # type: ignore[no-untyped-def]
    ev = evidence(
        _cand(), _inc(bid_due=None, project_name="Pitt Benedum Hall Lab Renovation - Addendum 2")
    )
    assert decide(ev, incoming_kind="addendum") == "merge"


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


def test_gap_detection_disabled_for_unorderable_labels():
    assert gap_detection_enabled(["Addendum 1", "Addendum 3"])
    assert not gap_detection_enabled(["Addendum A", "Bulletin 1"])
    assert not gap_detection_enabled(["Addendum 1", "Addendum A"])


def test_merge_scope_unions_and_removes_only_when_stated():
    out = merge_scope(["lighting"], ["fire_alarm"])
    assert out.value == ["fire_alarm", "lighting"] and out.added == ["fire_alarm"]
    # Silence never removes: an addendum that simply does not mention lighting keeps it.
    out = merge_scope(["lighting", "fire_alarm"], [], changes_described="Revises door hardware.")
    assert out.value == ["fire_alarm", "lighting"] and not out.removed
    out = merge_scope(
        ["lighting", "site_lighting", "fire_alarm"],
        [],
        changes_described="Site lighting is removed from this scope; fixture schedule revised.",
    )
    assert out.removed == ["site_lighting"] and "lighting" in out.value


def test_size_change_ratio_and_materiality():
    assert size_change_ratio({"square_feet": 100.0}, {"square_feet": 160.0}) == 0.6
    # A value appearing where there was none is new knowledge, not a change in size.
    assert size_change_ratio({}, {"square_feet": 160.0}) == 0.0
    assert material_change(old_due=DUE, new_due=DUE + timedelta(days=8))
    assert not material_change(old_due=DUE, new_due=DUE + timedelta(days=5))
    assert material_change(
        old_size={"stated_electrical_value": 1_000_000.0},
        new_size={"stated_electrical_value": 400_000.0},
    )


def test_award_outcome_language():
    assert award_outcome("Thank you for bidding; the project was awarded to another firm") == "lost"
    assert (
        award_outcome("We regret to inform you that you were not the successful bidder") == "lost"
    )
    assert award_outcome("The owner has cancelled the project.") == "cancelled"
    # Cancellation wins: nobody was awarded, so this is not a loss.
    assert award_outcome("Bid cancelled; no award will be made to any bidder") == "cancelled"
    assert award_outcome("We are pleased to award this work to Ferry Electric") == "won"
    assert award_outcome("Bids are under review.") is None


def test_with_change_count():
    assert (
        with_change_count("Due date moved Oct 16 → Oct 21", 1) == "Due date moved Oct 16 → Oct 21"
    )
    assert with_change_count("Due date moved Oct 21 → Oct 28", 4).endswith("(4 changes)")


def test_geohash_buckets_nearby_sites_together():
    # A known reference value, so the bit interleaving stays honest.
    assert geohash(57.64911, 10.40744, 11) == "u4pruydqqvj"
    # Cell edges mean neighbours can differ at full precision, which is why candidate
    # generation matches on a prefix and `evidence()` does the real distance test.
    downtown = geohash(40.4406, -79.9959)
    assert downtown[:5] == geohash(40.4410, -79.9950)[:5]
    assert downtown[:5] != geohash(40.5500, -80.1000)[:5]
    assert geohash(None, None) == ""


def test_names_that_disagree_veto_the_match():
    """One GC, one city, one month, two different jobs: corroboration must not outvote the name.

    Taken from a real pair in the fixture corpus that used to attach provisionally at 0.61.
    """
    ev = evidence(
        _cand(project_name="UPMC Mercy Pavilion Level 3 Build-Out", owner_name=None),
        _inc(
            project_name="Pittsburgh Distribution Center",
            bid_due=DUE + timedelta(days=10),
            lat=40.4435,
            lon=-79.958,
        ),
    )
    assert ev.raw_name_similarity < 0.5 and ev.score < 0.6
    assert any("disagree" in n for n in ev.notes)
    assert decide(ev, incoming_kind="itb") == "new"


def test_a_shared_thread_still_wins_over_the_name_floor():
    """A hard key is still a hard key: the veto only governs soft matching."""
    ev = evidence(
        _cand(project_name="UPMC Mercy Pavilion Level 3", thread_ids={"<t1@tcco.com>"}),
        _inc(project_name="Pittsburgh Distribution Center", thread_ids={"<t1@tcco.com>"}),
    )
    assert decide(ev, incoming_kind="addendum") == "merge"


def test_platform_ids_identify_the_package_not_the_gc_tenant():
    """A hard key merges at confidence 1.0, so it must not be shared by every job a GC posts.

    Procore's first path segment is the company id: `app.procore.com/2318842/...` is the same
    number for every solicitation that GC sends.
    """
    from bidtriage.worker.pipeline import platform_ids

    one = platform_ids(["https://app.procore.com/2318271/project/bidding/bid_packages/9981"])
    two = platform_ids(["https://app.procore.com/2318271/project/bidding/bid_packages/7744"])
    assert one == {"procore:9981"} and two == {"procore:7744"}
    assert not (one & two), "two packages from one company must not share a hard key"

    # A company-level link identifies nothing, so it contributes no key at all.
    assert platform_ids(["https://us02.procore.com/519999/project/home"]) == set()

    # Namespaced, so the same number on two platforms cannot collide.
    assert platform_ids(["https://app.buildingconnected.com/projects/6613f2a9c0de11/x"]) == {
        "bc:6613f2a9c0de11"
    }
    assert not (
        platform_ids(["https://app.procore.com/1/packages/500"])
        & platform_ids(["https://x.example.com/b?projectId=500"])
    )

    # The most specific id wins when a link carries both.
    assert platform_ids(["https://bids.example.com/x?projectId=ABC-1&bidPackageId=99"]) == {
        "pkg:99"
    }


def test_material_change_is_symmetric_about_the_threshold():
    """`timedelta.days` floors, so the same move read forward and backward must not disagree."""
    over = timedelta(days=7, hours=20)
    under = timedelta(days=6, hours=20)
    assert material_change(old_due=DUE, new_due=DUE + over)
    assert material_change(old_due=DUE, new_due=DUE - over)
    assert not material_change(old_due=DUE, new_due=DUE + under)
    assert not material_change(old_due=DUE, new_due=DUE - under)
    # Exactly the threshold is not "more than" it, in either direction.
    assert not material_change(old_due=DUE, new_due=DUE + timedelta(days=7))
    assert not material_change(old_due=DUE, new_due=DUE - timedelta(days=7))


def test_gap_detection_needs_one_consistent_numbering_scheme():
    """A digest that says "Addendum 2 not received" states it as fact, so it must be sure."""
    assert gap_detection_enabled(["Addendum 1", "Addendum 3"])
    assert gap_detection_enabled(["Addendum No. 1", "Addendum 3"])
    assert gap_detection_enabled(["Bulletin 1", "Bulletin 3"])
    # Two sequences sharing one opportunity: a missing number in either is not evidence of a gap.
    assert not gap_detection_enabled(["Bulletin 1", "Addendum 3"])
    assert not gap_detection_enabled(["Addendum A", "Bulletin 1"])
    assert not gap_detection_enabled(["Addendum 1", "Addendum A"])
