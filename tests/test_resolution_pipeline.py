"""SPEC-03 at the database level: matching, change tracking, status and the edge-case table.

Messages are built in code rather than from `tests/fixtures/messages`, so a case can state exactly
the one thing it is about (a reminder that disagrees, an addendum with a non-numeric label) without
adding a fixture to the extraction corpus that measures something else.
"""

from __future__ import annotations

import time
from datetime import timedelta

from sqlalchemy import select

from bidtriage.core.clock import FrozenClock, aware
from bidtriage.core.models import (
    GC,
    Addendum,
    FieldHistory,
    GCDomain,
    Opportunity,
    OpportunityKey,
    OpportunitySource,
    RawMessage,
    User,
)
from bidtriage.decisions.actions import apply_action
from bidtriage.resolution import Incoming
from bidtriage.worker import pipeline
from tests.resolution_helpers import DUE_AWARE, NOW, PITT, deliver


def _history(session, opp, field):  # type: ignore[no-untyped-def]
    return session.scalars(
        select(FieldHistory)
        .where(FieldHistory.opportunity_id == opp.id, FieldHistory.field == field)
        .order_by(FieldHistory.changed_at, FieldHistory.id)
    ).all()


# ----------------------------------------------------------------- acceptance criteria


def test_cross_channel_invite_and_email_make_one_opportunity(session, pjdick):  # type: ignore[no-untyped-def]
    clock = FrozenClock(NOW)
    _, first, d1 = deliver(
        session,
        clock,
        from_addr="notifications@buildingconnected.com",
        subject="PJ Dick invited you to bid",
        delivery_channel="buildingconnected",
    )
    clock.advance(hours=2)
    _, second, d2 = deliver(session, clock, subject="Benedum Hall Lab - electrical scope")
    assert d1 == "new" and d2 == "merge"
    assert second.id == first.id
    assert session.query(Opportunity).count() == 1
    roles = {
        s.role
        for s in session.scalars(
            select(OpportunitySource).where(OpportunitySource.opportunity_id == first.id)
        ).all()
    }
    assert len(roles) >= 1
    assert (
        session.query(OpportunitySource)
        .filter(OpportunitySource.opportunity_id == first.id)
        .count()
        == 2
    )
    # Both delivery channels stay visible on the one opportunity.
    assert first.canonical["delivery_channels"] == ["buildingconnected", "email"]
    assert "also arrived via email" in (first.change_summary or "")


def test_date_change_records_history_and_summary(session, pjdick):  # type: ignore[no-untyped-def]
    clock = FrozenClock(NOW)
    _, opp, _ = deliver(session, clock)
    clock.advance(days=3)
    deliver(
        session,
        clock,
        kind="date_change",
        subject="Bid date extended",
        bid_due={"value": "2026-10-21T14:00:00"},
    )
    assert opp.canonical["bid_due"]["value"].startswith("2026-10-21T14:00")
    hist = _history(session, opp, "bid_due")
    assert len(hist) == 1 and hist[0].applied and hist[0].message_id
    # The wording SPEC-03's acceptance criterion asks for, not the column name.
    assert opp.change_summary == "Due date moved Oct 16 → Oct 21"
    assert opp.changed_since_digest


def test_reminder_may_confirm_but_never_move_a_date(session, pjdick):  # type: ignore[no-untyped-def]
    clock = FrozenClock(NOW)
    _, opp, _ = deliver(session, clock)
    clock.advance(days=5)
    deliver(
        session,
        clock,
        kind="reminder",
        subject="Reminder: bids due 10/17",
        bid_due={"value": "2026-10-17T14:00:00"},
    )
    assert opp.canonical["bid_due"]["value"].startswith("2026-10-16T14:00")
    assert "date_conflict" in opp.flags
    hist = _history(session, opp, "bid_due")
    assert len(hist) == 1 and not hist[0].applied
    assert hist[0].old["value"].startswith("2026-10-16") and hist[0].new["value"].startswith(
        "2026-10-17"
    )


def test_addendum_gap_is_flagged(session, pjdick):  # type: ignore[no-untyped-def]
    clock = FrozenClock(NOW)
    _, opp, _ = deliver(session, clock)
    clock.advance(days=2)
    deliver(session, clock, kind="addendum", addendum_label="Addendum 1", addendum_number=1)
    clock.advance(days=2)
    deliver(session, clock, kind="addendum", addendum_label="Addendum 3", addendum_number=3)
    assert "addendum_gap" in opp.flags
    assert {a.label for a in session.scalars(select(Addendum)).all()} == {
        "Addendum 1",
        "Addendum 3",
    }
    # Once the missing one arrives the flag clears.
    clock.advance(days=1)
    deliver(session, clock, kind="addendum", addendum_label="Addendum 2", addendum_number=2)
    assert "addendum_gap" not in opp.flags


def test_two_gcs_on_one_project_are_two_linked_opportunities(session, pjdick):  # type: ignore[no-untyped-def]
    clock = FrozenClock(NOW)
    other = GC(canonical_name="Mascaro", kind="gc", created_from="seed", created_at=NOW)
    session.add(other)
    session.flush()
    session.add(GCDomain(gc_id=other.id, domain="mascaroconstruction.com"))
    session.flush()
    _, first, _ = deliver(session, clock, project_name={"value": "UPMC Passavant ED Expansion"})
    clock.advance(hours=3)
    _, second, decision = deliver(
        session,
        clock,
        from_addr="bids@mascaroconstruction.com",
        gc_name={"value": "Mascaro"},
        gc_contacts=[{"name": "Pat", "email": "bids@mascaroconstruction.com"}],
        project_name={"value": "UPMC Passavant ED Expansion"},
    )
    assert decision == "related" and second.id != first.id
    assert session.query(Opportunity).count() == 2
    assert second.related_project_ids == [first.id]
    assert first.related_project_ids == [second.id]


def test_locked_field_is_never_overwritten(session, pjdick):  # type: ignore[no-untyped-def]
    clock = FrozenClock(NOW)
    _, opp, _ = deliver(session, clock)
    opp.locked_fields = ["bid_due"]
    session.flush()
    clock.advance(days=1)
    deliver(
        session,
        clock,
        kind="date_change",
        bid_due={"value": "2026-10-15T14:00:00"},
    )
    assert opp.canonical["bid_due"]["value"].startswith("2026-10-16T14:00")
    hist = _history(session, opp, "bid_due")
    assert len(hist) == 1 and not hist[0].applied
    assert hist[0].new["value"].startswith("2026-10-15")
    # A locked field is its own kind of disagreement: `date_conflict` is reserved for two sources
    # contradicting each other (SPEC-03 F3, the reminder rule).
    assert "locked_conflict" in opp.flags and "date_conflict" not in opp.flags


def test_orphan_update_creates_a_visible_stub(session, pjdick):  # type: ignore[no-untyped-def]
    clock = FrozenClock(NOW)
    _, opp, decision = deliver(
        session,
        clock,
        kind="addendum",
        project_name={"value": "A Job We Never Saw"},
        addendum_label="Addendum 2",
        addendum_number=2,
    )
    assert decision == "new"
    assert opp.status == "new" and "orphan_update" in opp.flags
    assert session.query(Addendum).count() == 1


# ----------------------------------------------------------------- edge-case table


def test_rebid_not_merged(session, pjdick):  # type: ignore[no-untyped-def]
    """Same name, same GC, due dates 60 days apart: two opportunities, linked."""
    clock = FrozenClock(NOW)
    _, first, _ = deliver(session, clock)
    clock.advance(days=1)
    _, second, decision = deliver(
        session, clock, subject="Benedum Hall Lab - rebid", bid_due={"value": "2026-12-15T14:00:00"}
    )
    assert decision == "related"
    assert second.id != first.id and session.query(Opportunity).count() == 2
    assert first.id in second.related_project_ids and "rebid_of_related" in second.flags


def test_channel_switch(session, pjdick):  # type: ignore[no-untyped-def]
    """BuildingConnected ITB, then an addendum from a personal address with no link."""
    clock = FrozenClock(NOW)
    _, first, _ = deliver(
        session,
        clock,
        from_addr="notifications@buildingconnected.com",
        delivery_channel="buildingconnected",
    )
    clock.advance(days=4)
    _, second, decision = deliver(
        session,
        clock,
        from_addr="jane.doe@gmail.com",
        subject="Addendum 1 for Benedum",
        kind="addendum",
        addendum_label="Addendum 1",
        addendum_number=1,
        gc_contacts=[{"name": "Jane Doe", "email": "jane.doe@gmail.com"}],
    )
    assert decision == "merge" and second.id == first.id


def test_generic_name_distinct_sites(session, pjdick):  # type: ignore[no-untyped-def]
    """The same generic name from one GC twice in a month, at addresses > 1 km apart."""
    clock = FrozenClock(NOW)
    _, first, _ = deliver(
        session,
        clock,
        project_name={"value": "Office Renovation"},
        location={"raw": "100 First Ave, Pittsburgh, PA", "city": "Pittsburgh", "state": "PA"},
        lat=40.4400,
        lon=-79.9950,
    )
    clock.advance(days=10)
    _, second, decision = deliver(
        session,
        clock,
        project_name={"value": "Office Renovation"},
        location={
            "raw": "9000 Steubenville Pike, Pittsburgh, PA",
            "city": "Pittsburgh",
            "state": "PA",
        },
        lat=40.4500,
        lon=-80.1500,
        bid_due={"value": "2026-10-30T14:00:00"},
    )
    assert decision == "new" and second.id != first.id
    assert session.query(Opportunity).count() == 2


def test_internal_reply(session, pjdick):  # type: ignore[no-untyped-def]
    """Reply-all chatter from Ferry's own estimator: a source with role internal, no field changes."""
    clock = FrozenClock(NOW)
    session.add(
        User(email="casey@ferryelectric.com", name="Casey", role="estimator", digest_prefs={})
    )
    session.flush()
    _, opp, _ = deliver(session, clock, message_id="<itb-1@pjdick.com>")
    before = dict(opp.canonical)
    clock.advance(hours=4)
    _, same, decision = deliver(
        session,
        clock,
        from_addr="casey@ferryelectric.com",
        subject="RE: Benedum Hall Lab Renovation - Electrical ITB",
        in_reply_to="<itb-1@pjdick.com>",
        references=["<itb-1@pjdick.com>"],
        bid_due={"value": "2026-11-30T14:00:00"},
        scope_items=["generator_ats"],
    )
    assert decision == "merge" and same.id == opp.id
    src = session.get(OpportunitySource, (opp.id, _last_message(session).id))
    assert src is not None and src.role == "internal"
    # Nothing an estimator wrote moved a date or added scope.
    assert opp.canonical["bid_due"]["value"] == before["bid_due"]["value"]
    assert opp.canonical["scope_items"] == before["scope_items"]
    assert not _history(session, opp, "bid_due")


def _last_message(session):  # type: ignore[no-untyped-def]
    return session.scalars(
        select(RawMessage).order_by(RawMessage.received_at.desc(), RawMessage.id.desc())
    ).first()


def test_multi_effect_message(session, pjdick):  # type: ignore[no-untyped-def]
    """One message that moves the date and carries Addendum 1 produces both effects."""
    clock = FrozenClock(NOW)
    _, opp, _ = deliver(session, clock)
    clock.advance(days=2)
    deliver(
        session,
        clock,
        kind="date_change",
        subject="Addendum 1 issued and bid date extended",
        addendum_label="Addendum 1",
        addendum_number=1,
        changes_described="Adds site lighting; extends the bid date to October 21.",
        bid_due={"value": "2026-10-21T14:00:00"},
    )
    assert len(_history(session, opp, "bid_due")) == 1
    assert session.query(Addendum).filter(Addendum.opportunity_id == opp.id).count() == 1
    assert "Addendum 1" in (opp.change_summary or "") and "moved" in (opp.change_summary or "")


def test_duplicate_addendum(session, pjdick):  # type: ignore[no-untyped-def]
    """The same addendum down two paths is one record with copies=2."""
    clock = FrozenClock(NOW)
    _, opp, _ = deliver(session, clock)
    for _ in range(2):
        clock.advance(hours=1)
        deliver(
            session,
            clock,
            kind="addendum",
            addendum_label="Addendum 1",
            addendum_number=1,
            changes_described="Revises the fixture schedule.",
        )
    rows = session.scalars(select(Addendum).where(Addendum.opportunity_id == opp.id)).all()
    assert len(rows) == 1 and rows[0].copies == 2
    assert len(_history(session, opp, "addenda")) == 1


def test_nonnumeric_addenda(session, pjdick):  # type: ignore[no-untyped-def]
    """Labels we cannot order are kept verbatim and switch gap detection off."""
    clock = FrozenClock(NOW)
    _, opp, _ = deliver(session, clock)
    for label in ("Addendum A", "Bulletin 1"):
        clock.advance(hours=2)
        deliver(session, clock, kind="addendum", addendum_label=label, addendum_number=None)
    clock.advance(hours=2)
    deliver(session, clock, kind="addendum", addendum_label="Addendum 3", addendum_number=3)
    labels = {a.label for a in session.scalars(select(Addendum)).all()}
    assert labels == {"Addendum A", "Bulletin 1", "Addendum 3"}
    assert "addendum_gap" not in opp.flags


def test_many_date_changes(session, pjdick):  # type: ignore[no-untyped-def]
    """Four moves: four history entries, one summary line carrying the count."""
    clock = FrozenClock(NOW)
    _, opp, _ = deliver(session, clock)
    for day in ("2026-10-21", "2026-10-23", "2026-10-28", "2026-11-04"):
        clock.advance(days=1)
        deliver(session, clock, kind="date_change", bid_due={"value": f"{day}T14:00:00"})
    hist = _history(session, opp, "bid_due")
    assert len(hist) == 4 and all(h.applied for h in hist)
    assert opp.change_summary == "Due date moved Oct 28 → Nov 04 (4 changes)"


def test_regret_sets_lost(session, pjdick):  # type: ignore[no-untyped-def]
    clock = FrozenClock(NOW)
    _, opp, _ = deliver(session, clock)
    apply_action(
        session, opp, "bid", actor_user_id=None, actor_role="chief", channel="review", clock=clock
    )
    apply_action(
        session,
        opp,
        "submit",
        actor_user_id=None,
        actor_role="chief",
        channel="review",
        clock=clock,
    )
    clock.advance(days=20)
    deliver(
        session,
        clock,
        kind="award",
        subject="Benedum Hall Lab - award notice",
        changes_described="Thank you for bidding; the work was awarded to another firm.",
    )
    assert opp.status == "lost"
    assert _history(session, opp, "status")[-1].new["value"] == "lost"
    # An estimator can still correct it.
    apply_action(
        session, opp, "won", actor_user_id=None, actor_role="chief", channel="review", clock=clock
    )
    assert opp.status == "won"


def test_cancel_passed(session, pjdick):  # type: ignore[no-untyped-def]
    """A cancellation for a job we passed changes status without making digest noise."""
    clock = FrozenClock(NOW)
    _, opp, _ = deliver(session, clock)
    apply_action(
        session,
        opp,
        "pass",
        actor_user_id=None,
        actor_role="chief",
        channel="review",
        reason="too_far",
        clock=clock,
    )
    opp.changed_since_digest, opp.material_change = False, False
    session.flush()
    clock.advance(days=4)
    deliver(
        session,
        clock,
        kind="award",
        subject="Project cancelled",
        changes_described="The owner has cancelled the project; no bids will be taken.",
    )
    assert opp.status == "cancelled"
    assert opp.changed_since_digest and not opp.material_change
    assert "cancelled by sender" in (opp.change_summary or "")


def test_snooze_vs_due(session, pjdick):  # type: ignore[no-untyped-def]
    """A date that moves in front of a snooze ends the snooze early, flagged."""
    clock = FrozenClock(NOW)
    _, opp, _ = deliver(session, clock, bid_due={"value": "2026-11-20T14:00:00"})
    apply_action(
        session,
        opp,
        "snooze",
        actor_user_id=None,
        actor_role="chief",
        channel="review",
        payload={"days": 21},
        clock=clock,
    )
    assert opp.status == "snoozed" and opp.snooze_until is not None
    clock.advance(days=1)
    deliver(session, clock, kind="date_change", bid_due={"value": "2026-10-09T14:00:00"})
    assert opp.status == "undecided" and opp.snooze_until is None
    assert "snooze_overridden" in opp.flags
    assert "snooze ended early" in (opp.change_summary or "")


def test_reactivate_archived(session, pjdick):  # type: ignore[no-untyped-def]
    """A reply on an archived opportunity's thread brings it back rather than forking it."""
    clock = FrozenClock(NOW)
    _, opp, _ = deliver(session, clock, message_id="<itb-9@pjdick.com>")
    opp.status, opp.archived_at = "archived", clock.now()
    session.flush()
    clock.advance(days=30)
    _, same, decision = deliver(
        session,
        clock,
        kind="addendum",
        subject="RE: Benedum Hall Lab - Addendum 1",
        in_reply_to="<itb-9@pjdick.com>",
        references=["<itb-9@pjdick.com>"],
        addendum_label="Addendum 1",
        addendum_number=1,
    )
    assert decision == "merge" and same.id == opp.id
    assert opp.archived_at is None and opp.status == "undecided"
    assert "reactivated" in opp.flags
    assert session.query(Opportunity).count() == 1


def test_matching_performance(session, pjdick):  # type: ignore[no-untyped-def]
    """500 live opportunities in the candidate window: matching stays well inside 2 s a message."""
    clock = FrozenClock(NOW)
    _, first, _ = deliver(session, clock)
    for n in range(500):
        session.add(
            Opportunity(
                status="new",
                gc_id=pjdick.id,
                canonical={
                    "project_name": {"value": f"Filler Job {n}", "confidence": 0.9},
                    "gc_name": {"value": "PJ Dick", "confidence": 0.9},
                    "gc_domain": "pjdick.com",
                    "bid_due": {"value": DUE_AWARE, "confidence": 0.9},
                    "location": PITT,
                    "scope_items": [],
                },
                normalized_name=f"filler job {n}",
                fingerprint=f"fp{n}",
                lat=40.44,
                lon=-79.95,
                geohash="dppn5",
                first_seen_at=NOW,
                last_activity_at=NOW,
            )
        )
    session.flush()
    clock.advance(days=1)
    started = time.perf_counter()
    _, matched, decision = deliver(
        session, clock, kind="date_change", bid_due={"value": "2026-10-21T14:00:00"}
    )
    elapsed = time.perf_counter() - started
    assert decision == "merge" and matched.id == first.id
    assert elapsed < 2.0, f"matching one message against 501 opportunities took {elapsed:.2f}s"


# ----------------------------------------------------------------- housekeeping


def test_hard_keys_are_indexed_for_lookup(session, pjdick):  # type: ignore[no-untyped-def]
    clock = FrozenClock(NOW)
    _, opp, _ = deliver(session, clock, message_id="<itb-3@pjdick.com>")
    keys = session.scalars(
        select(OpportunityKey).where(OpportunityKey.opportunity_id == opp.id)
    ).all()
    assert ("thread", "<itb-3@pjdick.com>") in {(k.kind, k.value) for k in keys}


def test_scope_removal_needs_words(session, pjdick):  # type: ignore[no-untyped-def]
    clock = FrozenClock(NOW)
    _, opp, _ = deliver(session, clock, scope_items=["lighting", "site_lighting"])
    clock.advance(days=1)
    deliver(
        session,
        clock,
        kind="addendum",
        addendum_label="Addendum 1",
        addendum_number=1,
        scope_items=[],
        changes_described="Site lighting is removed from the electrical scope.",
    )
    assert opp.canonical["scope_items"] == ["lighting"]
    assert "removed_by_addendum" in opp.flags


def test_archive_stale_retires_untouched_opportunities(session, pjdick):  # type: ignore[no-untyped-def]
    clock = FrozenClock(NOW)
    _, old, _ = deliver(session, clock)
    clock.advance(days=1)
    _, fresh, decision = deliver(
        session,
        clock,
        project_name={"value": "Greensburg Water Treatment Plant Upgrade"},
        location={"raw": "100 Main St, Greensburg, PA", "city": "Greensburg", "state": "PA"},
        lat=40.3015,
        lon=-79.5389,
        bid_due={"value": "2026-11-20T14:00:00"},
    )
    assert decision == "new" and fresh.id != old.id
    old.last_activity_at = NOW - timedelta(days=200)
    session.flush()
    assert pipeline.archive_stale(session, now=NOW) == 1
    assert old.status == "archived" and aware(old.archived_at) is not None
    assert fresh.status == "new"


def test_rfb_merges_with_an_earlier_itb_and_records_the_bid_type(session, pjdick):  # type: ignore[no-untyped-def]
    """SPEC-03 F2.4: the same GC re-issuing as a request for bid is the same opportunity."""
    clock = FrozenClock(NOW)
    _, opp, _ = deliver(session, clock, bid_type="budget")
    assert opp.canonical["bid_type"] == "budget"
    clock.advance(days=6)
    _, same, decision = deliver(
        session,
        clock,
        kind="rfb",
        subject="Benedum Hall Lab Renovation - request for bid",
        bid_type="hard_bid",
    )
    assert decision == "merge" and same.id == opp.id
    assert opp.canonical["bid_type"] == "hard_bid"
    hist = _history(session, opp, "bid_type")
    assert (
        len(hist) == 1 and hist[0].old["value"] == "budget" and hist[0].new["value"] == "hard_bid"
    )
    assert "bid type budget → hard_bid" in (opp.change_summary or "")


def test_passed_opportunity_still_receives_updates(session, pjdick):  # type: ignore[no-untyped-def]
    """SPEC-03 F5: a passed job keeps tracking, so a re-scoped one can be reconsidered."""
    clock = FrozenClock(NOW)
    _, opp, _ = deliver(session, clock, size_signals={"stated_electrical_value": 400000.0})
    apply_action(
        session,
        opp,
        "pass",
        actor_user_id=None,
        actor_role="chief",
        channel="review",
        reason="too_small",
        clock=clock,
    )
    opp.changed_since_digest, opp.material_change = False, False
    session.flush()
    clock.advance(days=3)
    deliver(
        session,
        clock,
        kind="addendum",
        addendum_label="Addendum 1",
        addendum_number=1,
        changes_described="Scope tripled; the electrical value is now $1.2M.",
        size_signals={"stated_electrical_value": 1200000.0},
    )
    assert opp.status == "passed"
    assert opp.canonical["size_signals"]["stated_electrical_value"] == 1200000.0
    assert opp.changed_since_digest and opp.material_change
    assert len(_history(session, opp, "size_signals")) == 1


def test_candidate_generation_degrades_without_pg_trgm(session, pjdick):  # type: ignore[no-untyped-def]
    """SQLite has no trigram search, so matching falls back to token LIKE — deliberately."""
    clock = FrozenClock(NOW)
    _, first, _ = deliver(session, clock)
    assert not pipeline._has_pg_trgm(session)
    clock.advance(days=2)
    # A name no exact key or fingerprint would catch still reaches `evidence()` through its tokens.
    _, same, decision = deliver(
        session,
        clock,
        kind="addendum",
        project_name={"value": "Benedum Hall Laboratory Renovation, Fourth Floor"},
        addendum_label="Addendum 1",
        addendum_number=1,
    )
    assert decision == "merge" and same.id == first.id


def test_project_number_and_gc_is_a_hard_key(session, pjdick):  # type: ignore[no-untyped-def]
    """SPEC-03 F2.1, and the JSON-path clause candidate generation uses to find it."""
    clock = FrozenClock(NOW)
    _, first, _ = deliver(session, clock, project_number={"value": "21-0148", "confidence": 0.9})
    clock.advance(days=9)
    _, same, decision = deliver(
        session,
        clock,
        subject="Job 21-0148 - revised bid form",
        project_name={"value": "Job 21-0148"},
        project_number={"value": "21-0148", "confidence": 0.9},
        location={"raw": None, "city": None, "state": None},
        lat=None,
        bid_due={"value": None},
        kind="addendum",
        addendum_label="Addendum 1",
        addendum_number=1,
    )
    assert decision == "merge" and same.id == first.id
    src = session.get(OpportunitySource, (first.id, _last_message(session).id))
    assert src is not None and src.evidence["hard_key"] == "project_number"


def test_candidate_generation_finds_nothing_for_an_unrelated_message(session, pjdick):  # type: ignore[no-untyped-def]
    """A message with no name, GC, place or date must not match everything we know."""
    clock = FrozenClock(NOW)
    deliver(session, clock)
    clock.advance(days=1)
    _, fresh, decision = deliver(
        session,
        clock,
        subject="Bid opportunity",
        from_addr="someone@unknown-gc.example",
        project_name={"value": None, "confidence": 0.0},
        gc_name={"value": None, "confidence": 0.0},
        gc_contacts=[],
        location={"raw": None, "city": None, "state": None},
        lat=None,
        bid_due={"value": None},
        summary="A bid invitation with nothing we can pin down.",
    )
    assert decision == "new" and session.query(Opportunity).count() == 2
    assert fresh.normalized_name == ""
    # And a record with nothing at all to fingerprint generates no candidates, rather than
    # matching every other record we also knew nothing about.
    empty = Incoming(
        project_name=None,
        gc_id=None,
        gc_domain=None,
        city=None,
        lat=None,
        lon=None,
        bid_due=None,
        owner_name=None,
    )
    assert pipeline.candidate_ids(session, empty, set()) == []


def test_regret_for_an_undecided_job_still_closes_it(session, pjdick):  # type: ignore[no-untyped-def]
    """An award notice must land even when nobody got round to deciding (SPEC-03 F5 edge cases)."""
    clock = FrozenClock(NOW)
    _, opp, _ = deliver(session, clock, message_id="<a@pjdick.com>")
    assert opp.status == "new"
    clock.advance(days=30)
    deliver(
        session,
        clock,
        kind="award",
        subject="Award notice",
        changes_described="Thank you for bidding; the work was awarded to another firm.",
        in_reply_to="<a@pjdick.com>",
        references=("<a@pjdick.com>",),
    )
    assert opp.status == "lost"
    assert _history(session, opp, "status")[-1].new["value"] == "lost"
    assert "marked lost by sender" in (opp.change_summary or "")


def test_cancellation_lands_on_a_job_already_won(session, pjdick):  # type: ignore[no-untyped-def]
    """F5 says `cancelled` applies from any status; an owner killing an awarded job is the case."""
    clock = FrozenClock(NOW)
    _, opp, _ = deliver(session, clock, message_id="<b@pjdick.com>")
    opp.status = "won"
    session.flush()
    clock.advance(days=5)
    deliver(
        session,
        clock,
        kind="award",
        subject="Project cancelled",
        changes_described="The owner has cancelled the project.",
        in_reply_to="<b@pjdick.com>",
        references=("<b@pjdick.com>",),
    )
    assert opp.status == "cancelled"
    assert "cancelled by sender" in (opp.change_summary or "")


def test_a_refused_outcome_is_recorded_not_dropped(session, pjdick):  # type: ignore[no-untyped-def]
    """We cannot win what we never bid, so the claim is surfaced instead of applied or ignored."""
    clock = FrozenClock(NOW)
    _, opp, _ = deliver(session, clock, message_id="<c@pjdick.com>")
    clock.advance(days=20)
    deliver(
        session,
        clock,
        kind="award",
        subject="Award",
        changes_described="We are pleased to award this work to Ferry Electric.",
        in_reply_to="<c@pjdick.com>",
        references=("<c@pjdick.com>",),
    )
    assert opp.status == "new", "a job we never bid must not silently become won"
    unapplied = [h for h in _history(session, opp, "status") if not h.applied]
    assert len(unapplied) == 1 and unapplied[0].new["value"] == "won"
    assert unapplied[0].message_id is not None
    assert "outcome_conflict" in opp.flags
    assert "sender says won, but this is new" in (opp.change_summary or "")
    assert opp.changed_since_digest


def test_award_records_an_outcome_with_its_source(session, pjdick):  # type: ignore[no-untyped-def]
    """SPEC-03 edge cases: "outcome recorded with source", so a correction can see what was read."""
    from bidtriage.core.models import Outcome

    clock = FrozenClock(NOW)
    _, opp, _ = deliver(session, clock, message_id="<a@pjdick.com>")
    clock.advance(days=25)
    award, _, _ = deliver(
        session,
        clock,
        kind="award",
        subject="Award notice",
        changes_described="Thank you for bidding; the work was awarded to another firm.",
        in_reply_to="<a@pjdick.com>",
        references=("<a@pjdick.com>",),
    )
    outcome = session.scalars(select(Outcome).where(Outcome.opportunity_id == opp.id)).one()
    assert outcome.result == "lost"
    assert outcome.source_message_id == award.id
    assert "another firm" in (outcome.notes or "")


def test_manual_status_changes_are_tracked_too(session, pjdick):  # type: ignore[no-untyped-def]
    """SPEC-03 F4 tracks `status` whoever moved it, so the detail page shows both paths."""
    clock = FrozenClock(NOW)
    _, opp, _ = deliver(session, clock)
    for action in ("bid", "submit"):
        # Separated in time: `field_history` orders by `changed_at`, and two rows sharing an
        # instant have no defined order (the id is a random UUID).
        clock.advance(hours=2)
        apply_action(
            session,
            opp,
            action,
            actor_user_id=None,
            actor_role="chief",
            channel="review",
            clock=clock,
        )
    hist = _history(session, opp, "status")
    assert [(h.old["value"], h.new["value"]) for h in hist] == [
        ("new", "bidding"),
        ("bidding", "submitted"),
    ]
    assert all(h.applied for h in hist)


def test_two_procore_packages_from_one_gc_stay_separate(session, pjdick):  # type: ignore[no-untyped-def]
    """The regression that matters most: a shared tenant id merged two unrelated jobs at 1.0.

    `app.procore.com/2318842/...` — that first path segment is the GC's company id and is the same
    for every solicitation they send, so taking it as a platform key was a guaranteed false merge.
    """
    clock = FrozenClock(NOW)
    _, first, _ = deliver(
        session,
        clock,
        project_name={"value": "Mercy Pavilion Level 3"},
        links=("https://app.procore.com/2318842/bidding/packages/9912",),
        message_id="<a@pjdick.com>",
    )
    keys = {
        (k.kind, k.value)
        for k in session.scalars(
            select(OpportunityKey).where(OpportunityKey.opportunity_id == first.id)
        ).all()
    }
    assert ("platform", "procore:9912") in keys, keys

    clock.advance(days=1)
    _, second, decision = deliver(
        session,
        clock,
        subject="ITB - Pittsburgh Distribution Center",
        project_name={"value": "Pittsburgh Distribution Center"},
        links=("https://app.procore.com/2318842/bidding/packages/7744",),
        location={"raw": "500 Steel St, Coraopolis, PA", "city": "Coraopolis", "state": "PA"},
        lat=40.55,
        lon=-80.15,
        bid_due={"value": "2026-11-20T14:00:00"},
        message_id="<b@pjdick.com>",
    )
    assert decision == "new" and second.id != first.id
    assert session.query(Opportunity).count() == 2
    # The first job keeps its own bid date instead of inheriting the second one's.
    assert first.canonical["bid_due"]["value"].startswith("2026-10-16")


def test_the_same_package_link_on_two_messages_is_one_opportunity(session, pjdick):  # type: ignore[no-untyped-def]
    """The other half: a genuine shared package id is still a hard key (SPEC-03 F2.1)."""
    clock = FrozenClock(NOW)
    _, first, _ = deliver(
        session,
        clock,
        links=("https://app.procore.com/2318842/bidding/packages/9912",),
        message_id="<a@pjdick.com>",
    )
    clock.advance(days=3)
    _, same, decision = deliver(
        session,
        clock,
        subject="Reminder from a different address",
        from_addr="assistant@pjdick.com",
        project_name={"value": "Something Worded Completely Differently"},
        links=("https://app.procore.com/2318842/bidding/packages/9912",),
        message_id="<c@pjdick.com>",
    )
    assert decision == "merge" and same.id == first.id


def test_an_addendum_with_no_number_is_still_recorded(session, pjdick):  # type: ignore[no-untyped-def]
    """ "Revised drawings issued" with no label and no number must not vanish."""
    clock = FrozenClock(NOW)
    _, opp, _ = deliver(session, clock, message_id="<a@pjdick.com>")
    opp.changed_since_digest = False
    session.flush()
    clock.advance(days=2)
    deliver(
        session,
        clock,
        kind="addendum",
        subject="Revised drawings issued",
        addendum_label=None,
        addendum_number=None,
        changes_described="Revised drawings issued for the electrical rooms.",
        in_reply_to="<a@pjdick.com>",
        references=("<a@pjdick.com>",),
    )
    rows = session.scalars(select(Addendum).where(Addendum.opportunity_id == opp.id)).all()
    assert len(rows) == 1 and rows[0].number is None
    assert "Revised drawings issued" in rows[0].label
    assert opp.changed_since_digest and "received" in (opp.change_summary or "")


def test_a_naive_stored_date_does_not_break_resolution(session, pjdick):  # type: ignore[no-untyped-def]
    """A canonical record written by hand must not permanently fail the resolve job."""
    clock = FrozenClock(NOW)
    _, opp, _ = deliver(session, clock, message_id="<a@pjdick.com>")
    c = dict(opp.canonical)
    c["bid_due"] = {**c["bid_due"], "value": "2026-10-16T14:00:00"}  # no offset
    opp.canonical = c
    session.flush()
    clock.advance(days=2)
    _, same, decision = deliver(
        session,
        clock,
        kind="date_change",
        bid_due={"value": "2026-10-30T14:00:00"},
        in_reply_to="<a@pjdick.com>",
        references=("<a@pjdick.com>",),
    )
    assert decision == "merge" and same.id == opp.id
    assert opp.canonical["bid_due"]["value"].startswith("2026-10-30")
    assert opp.material_change, "a two-week move is material"
