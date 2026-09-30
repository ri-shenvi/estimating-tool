"""Manual merge, split and undo (SPEC-03 F6).

The promise the tests hold the code to is that undo restores the previous state *exactly*: the same
source rows with the same roles, the same history rows with their original ids and timestamps, and
the same statuses.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from bidtriage.core.clock import FrozenClock
from bidtriage.core.models import (
    Addendum,
    AuditEvent,
    Decision,
    FieldHistory,
    MergeLog,
    Opportunity,
    OpportunityKey,
    OpportunitySource,
)
from bidtriage.decisions.actions import apply_action
from bidtriage.worker import curation, pipeline
from tests.resolution_helpers import NOW, deliver


def _sources(session, opp_id):  # type: ignore[no-untyped-def]
    return {
        (s.message_id, s.role)
        for s in session.scalars(
            select(OpportunitySource).where(OpportunitySource.opportunity_id == opp_id)
        ).all()
    }


def _two_opportunities(session, clock):  # type: ignore[no-untyped-def]
    """Two opportunities the matcher deliberately keeps apart: a rebid of the same job."""
    _, first, _ = deliver(session, clock)
    clock.advance(days=1)
    _, second, decision = deliver(
        session, clock, subject="Benedum rebid", bid_due={"value": "2026-12-15T14:00:00"}
    )
    assert decision == "related" and first.id != second.id
    return first, second


def test_merge_moves_sources_history_and_logs(session, pjdick):  # type: ignore[no-untyped-def]
    clock = FrozenClock(NOW)
    survivor, victim = _two_opportunities(session, clock)
    clock.advance(days=1)
    deliver(
        session,
        clock,
        subject="Benedum rebid - Addendum 1",
        kind="addendum",
        addendum_label="Addendum 1",
        addendum_number=1,
        bid_due={"value": "2026-12-20T14:00:00"},
    )
    victim_sources = _sources(session, victim.id)
    assert len(victim_sources) == 2

    entry = curation.merge_opportunities(
        session, survivor_id=survivor.id, victim_id=victim.id, reason="same job", clock=clock
    )
    assert session.get(Opportunity, victim.id) is None
    assert session.query(Opportunity).count() == 1
    assert victim_sources <= _sources(session, survivor.id)
    assert all(a.opportunity_id == survivor.id for a in session.scalars(select(Addendum)).all())
    assert all(h.opportunity_id == survivor.id for h in session.scalars(select(FieldHistory)).all())
    assert all(
        k.opportunity_id == survivor.id for k in session.scalars(select(OpportunityKey)).all()
    )
    assert entry.kind == "merge" and entry.survivor_id == survivor.id
    assert survivor.changed_since_digest and "merged with" in (survivor.change_summary or "")
    assert (
        session.scalar(select(AuditEvent).where(AuditEvent.action == "opportunity.merge"))
        is not None
    )


def test_merge_then_undo_restores_exactly(session, pjdick):  # type: ignore[no-untyped-def]
    clock = FrozenClock(NOW)
    survivor, victim = _two_opportunities(session, clock)
    clock.advance(days=1)
    deliver(
        session,
        clock,
        subject="Benedum rebid - date change",
        kind="date_change",
        bid_due={"value": "2026-12-20T14:00:00"},
    )
    apply_action(
        session,
        victim,
        "pass",
        actor_user_id=None,
        actor_role="chief",
        channel="review",
        reason="too_far",
        clock=clock,
    )
    before = {
        "survivor": {
            "sources": _sources(session, survivor.id),
            "status": survivor.status,
            "canonical": dict(survivor.canonical),
            "flags": list(survivor.flags),
            "related": list(survivor.related_project_ids),
        },
        "victim": {
            "sources": _sources(session, victim.id),
            "status": victim.status,
            "canonical": dict(victim.canonical),
            "history": {h.id for h in _all_history(session, victim.id)},
            "decisions": {d.id for d in _all_decisions(session, victim.id)},
            "keys": {(k.kind, k.value) for k in _all_keys(session, victim.id)},
        },
    }
    victim_id = victim.id

    entry = curation.merge_opportunities(
        session, survivor_id=survivor.id, victim_id=victim_id, clock=clock
    )
    clock.advance(days=2)
    curation.undo(session, merge_log_id=entry.id, clock=clock)

    restored = session.get(Opportunity, victim_id)
    assert restored is not None
    assert restored.status == before["victim"]["status"]
    assert restored.canonical == before["victim"]["canonical"]
    assert _sources(session, victim_id) == before["victim"]["sources"]
    assert {h.id for h in _all_history(session, victim_id)} == before["victim"]["history"]
    assert {d.id for d in _all_decisions(session, victim_id)} == before["victim"]["decisions"]
    assert {(k.kind, k.value) for k in _all_keys(session, victim_id)} == before["victim"]["keys"]

    assert _sources(session, survivor.id) == before["survivor"]["sources"]
    assert survivor.status == before["survivor"]["status"]
    assert survivor.canonical == before["survivor"]["canonical"]
    assert survivor.flags == before["survivor"]["flags"]
    assert survivor.related_project_ids == before["survivor"]["related"]
    assert session.get(MergeLog, entry.id).undone_at is not None


def _all_history(session, opp_id):  # type: ignore[no-untyped-def]
    return session.scalars(select(FieldHistory).where(FieldHistory.opportunity_id == opp_id)).all()


def _all_decisions(session, opp_id):  # type: ignore[no-untyped-def]
    return session.scalars(select(Decision).where(Decision.opportunity_id == opp_id)).all()


def _all_keys(session, opp_id):  # type: ignore[no-untyped-def]
    return session.scalars(
        select(OpportunityKey).where(OpportunityKey.opportunity_id == opp_id)
    ).all()


def test_merge_conflicting_decisions(session, pjdick):  # type: ignore[no-untyped-def]
    """Two opportunities carrying different decisions cannot be merged silently."""
    clock = FrozenClock(NOW)
    survivor, victim = _two_opportunities(session, clock)
    apply_action(
        session,
        survivor,
        "bid",
        actor_user_id=None,
        actor_role="chief",
        channel="review",
        clock=clock,
    )
    apply_action(
        session,
        victim,
        "pass",
        actor_user_id=None,
        actor_role="chief",
        channel="review",
        reason="too_far",
        clock=clock,
    )
    with pytest.raises(curation.MergeConflictError) as e:
        curation.merge_opportunities(
            session, survivor_id=survivor.id, victim_id=victim.id, clock=clock
        )
    assert "bidding" in str(e.value) and "passed" in str(e.value)
    assert session.query(Opportunity).count() == 2
    assert session.query(MergeLog).count() == 0
    # Agreeing on the decision unblocks it.
    apply_action(
        session,
        victim,
        "bid",
        actor_user_id=None,
        actor_role="chief",
        channel="review",
        clock=clock,
    )
    curation.merge_opportunities(session, survivor_id=survivor.id, victim_id=victim.id, clock=clock)
    assert session.query(Opportunity).count() == 1


def test_merge_absorbs_a_duplicate_addendum_and_undo_splits_it_again(session, pjdick):  # type: ignore[no-untyped-def]
    clock = FrozenClock(NOW)
    survivor, victim = _two_opportunities(session, clock)
    for opp_subject in ("Benedum Hall Lab Renovation - Electrical ITB", "Benedum rebid"):
        clock.advance(hours=3)
        deliver(
            session,
            clock,
            subject=f"{opp_subject} - Addendum 1",
            kind="addendum",
            addendum_label="Addendum 1",
            addendum_number=1,
            bid_due={"value": "2026-10-16T14:00:00"}
            if "Electrical ITB" in opp_subject
            else {"value": "2026-12-15T14:00:00"},
        )
    assert session.query(Addendum).count() == 2
    entry = curation.merge_opportunities(
        session, survivor_id=survivor.id, victim_id=victim.id, clock=clock
    )
    rows = session.scalars(select(Addendum)).all()
    assert len(rows) == 1 and rows[0].copies == 2
    curation.undo(session, merge_log_id=entry.id, clock=clock)
    rows = session.scalars(select(Addendum)).all()
    assert len(rows) == 2 and {r.copies for r in rows} == {1}


def test_split_moves_one_source_out(session, pjdick):  # type: ignore[no-untyped-def]
    clock = FrozenClock(NOW)
    _, opp, _ = deliver(session, clock)
    clock.advance(days=2)
    stray, same, decision = deliver(
        session,
        clock,
        subject="Benedum Hall Lab - phase 2 scope",
        kind="addendum",
        addendum_label="Addendum 1",
        addendum_number=1,
    )
    assert decision == "merge" and same.id == opp.id
    entry = curation.split_source(session, opportunity_id=opp.id, message_id=stray.id, clock=clock)
    fresh = session.get(Opportunity, entry.other_id)
    assert fresh is not None and "split_out" in fresh.flags
    assert _sources(session, opp.id) == {
        (m, r) for m, r in _sources(session, opp.id) if m != stray.id
    }
    assert (stray.id, "addendum") in _sources(session, fresh.id)
    assert all(a.opportunity_id == fresh.id for a in session.scalars(select(Addendum)).all())
    assert fresh.id in opp.related_project_ids and opp.id in fresh.related_project_ids


def test_split_then_undo_restores_exactly(session, pjdick):  # type: ignore[no-untyped-def]
    clock = FrozenClock(NOW)
    _, opp, _ = deliver(session, clock)
    clock.advance(days=2)
    stray, _, _ = deliver(
        session,
        clock,
        subject="Benedum Hall Lab - Addendum 1",
        kind="addendum",
        addendum_label="Addendum 1",
        addendum_number=1,
    )
    before = {
        "sources": _sources(session, opp.id),
        "status": opp.status,
        "canonical": dict(opp.canonical),
        "history": {h.id for h in _all_history(session, opp.id)},
        "addenda": {a.id for a in session.scalars(select(Addendum)).all()},
        "keys": {(k.kind, k.value) for k in _all_keys(session, opp.id)},
        "related": list(opp.related_project_ids),
    }
    entry = curation.split_source(session, opportunity_id=opp.id, message_id=stray.id, clock=clock)
    clock.advance(days=1)
    curation.undo(session, merge_log_id=entry.id, clock=clock)
    assert session.get(Opportunity, entry.other_id) is None
    assert session.query(Opportunity).count() == 1
    assert _sources(session, opp.id) == before["sources"]
    assert opp.status == before["status"] and opp.canonical == before["canonical"]
    assert {h.id for h in _all_history(session, opp.id)} == before["history"]
    assert {a.id for a in session.scalars(select(Addendum)).all()} == before["addenda"]
    assert {(k.kind, k.value) for k in _all_keys(session, opp.id)} == before["keys"]
    assert opp.related_project_ids == before["related"]


def test_split_refuses_the_only_source(session, pjdick):  # type: ignore[no-untyped-def]
    clock = FrozenClock(NOW)
    msg, opp, _ = deliver(session, clock)
    with pytest.raises(ValueError, match="only source"):
        curation.split_source(session, opportunity_id=opp.id, message_id=msg.id, clock=clock)


def test_undo_expires_after_the_window(session, pjdick):  # type: ignore[no-untyped-def]
    clock = FrozenClock(NOW)
    survivor, victim = _two_opportunities(session, clock)
    entry = curation.merge_opportunities(
        session, survivor_id=survivor.id, victim_id=victim.id, clock=clock
    )
    clock.advance(days=31)
    with pytest.raises(curation.UndoExpiredError):
        curation.undo(session, merge_log_id=entry.id, clock=clock, window_days=30)
    assert curation.undoable(session, now=clock.now(), window_days=30) == []
    # And inside the window it is offered.
    assert [m.id for m in curation.undoable(session, now=NOW + timedelta(days=2))] == [entry.id]


def test_undo_is_not_repeatable(session, pjdick):  # type: ignore[no-untyped-def]
    clock = FrozenClock(NOW)
    survivor, victim = _two_opportunities(session, clock)
    entry = curation.merge_opportunities(
        session, survivor_id=survivor.id, victim_id=victim.id, clock=clock
    )
    curation.undo(session, merge_log_id=entry.id, clock=clock)
    with pytest.raises(ValueError, match="already undone"):
        curation.undo(session, merge_log_id=entry.id, clock=clock)


def test_merge_into_self_is_refused(session, pjdick):  # type: ignore[no-untyped-def]
    clock = FrozenClock(NOW)
    _, opp, _ = deliver(session, clock)
    with pytest.raises(ValueError, match="into itself"):
        curation.merge_opportunities(session, survivor_id=opp.id, victim_id=opp.id, clock=clock)


def test_split_over_the_api(session, pjdick):  # type: ignore[no-untyped-def]
    """The review page's split button, through the JSON API (SPEC-03 F6)."""
    from fastapi.testclient import TestClient

    from bidtriage.core.config import Settings, get_settings
    from bidtriage.web.app import create_app
    from bidtriage.web.deps import db

    clock = FrozenClock(NOW)
    _, opp, _ = deliver(session, clock)
    clock.advance(days=2)
    stray, _, _ = deliver(
        session,
        clock,
        subject="Benedum Hall Lab - Addendum 1",
        kind="addendum",
        addendum_label="Addendum 1",
        addendum_number=1,
    )
    app = create_app()

    def _db():  # type: ignore[no-untyped-def]
        yield session
        session.flush()

    app.dependency_overrides[db] = _db
    app.dependency_overrides[get_settings] = lambda: Settings(BIDTRIAGE_ENV="dev")
    client = TestClient(app)

    listed = client.get(f"/api/opportunities/{opp.id}/sources").json()
    assert {r["message_id"] for r in listed} == {m for m, _ in _sources(session, opp.id)}

    r = client.post(f"/api/opportunities/{opp.id}/split", json={"message_id": stray.id})
    assert r.status_code == 200, r.text
    fresh_id = r.json()["new_opportunity_id"]
    assert session.get(Opportunity, fresh_id) is not None
    log_id = r.json()["merge_log_id"]
    assert client.post(f"/api/merges/{log_id}/undo").status_code == 200
    assert session.get(Opportunity, fresh_id) is None


def test_detail_page_renders_sources_addenda_and_related(session, pjdick):  # type: ignore[no-untyped-def]
    """The SPEC-03 blocks on the opportunity page render with real rows behind them."""
    from fastapi.testclient import TestClient

    from bidtriage.core.config import Settings, get_settings
    from bidtriage.core.models import User
    from bidtriage.web.app import create_app
    from bidtriage.web.deps import db

    clock = FrozenClock(NOW)
    session.add(User(email="casey@example.com", name="Casey", role="chief", digest_prefs={}))
    survivor, other = _two_opportunities(session, clock)
    clock.advance(days=1)
    addendum_msg, _, _ = deliver(
        session,
        clock,
        subject="Benedum Hall Lab - Addendum 1",
        kind="addendum",
        addendum_label="Addendum 1",
        addendum_number=1,
    )
    # Splitting a source out leaves two near-identical opportunities, so each is the other's best
    # merge candidate — the case the picker exists for. The addendum goes with its message.
    entry = curation.split_source(
        session, opportunity_id=survivor.id, message_id=addendum_msg.id, clock=clock
    )
    twin = session.get(Opportunity, entry.other_id)
    assert twin is not None
    assert [o.id for o, _ in pipeline.possible_duplicates(session, twin)][:1] == [survivor.id]
    session.flush()
    app = create_app()

    def _db():  # type: ignore[no-untyped-def]
        yield session
        session.flush()

    app.dependency_overrides[db] = _db
    app.dependency_overrides[get_settings] = lambda: Settings(BIDTRIAGE_ENV="dev")
    client = TestClient(app)

    body = client.get(f"/opportunities/{twin.id}").text
    assert "Sources" in body and "Addenda" in body
    assert "Possible duplicates" in body and "merge into this" in body
    assert "Related projects" in body
    assert "split out" in body
    index = client.get("/").text
    assert "Recent merges and splits" in index and "undo" in index


def test_split_moves_hard_keys_to_the_right_side_without_autoflush(session, pjdick):  # type: ignore[no-untyped-def]
    """Each message's thread key follows it, and the split does not rely on autoflush being on."""
    session.autoflush = False
    clock = FrozenClock(NOW)
    _, opp, _ = deliver(session, clock, message_id="<a@pjdick.com>")
    clock.advance(days=1)
    stray, _, decision = deliver(
        session,
        clock,
        subject="Benedum Hall Lab - Addendum 1",
        kind="addendum",
        addendum_label="Addendum 1",
        addendum_number=1,
        message_id="<b@pjdick.com>",
    )
    assert decision == "merge"
    entry = curation.split_source(session, opportunity_id=opp.id, message_id=stray.id, clock=clock)
    assert {k.value for k in _all_keys(session, opp.id)} == {"<a@pjdick.com>"}
    assert {k.value for k in _all_keys(session, entry.other_id)} == {"<b@pjdick.com>"}
    curation.undo(session, merge_log_id=entry.id, clock=clock)
    assert {k.value for k in _all_keys(session, opp.id)} == {
        "<a@pjdick.com>",
        "<b@pjdick.com>",
    }


def test_undo_split_absorbs_an_addendum_added_afterwards(session, pjdick):  # type: ignore[no-untyped-def]
    """Work done on the split-out opportunity comes home without violating a unique constraint.

    `addenda` is unique on (opportunity, label). The merge path absorbs label twins; undoing a
    split has to do the same, or the undo raises and the two halves are stuck apart.
    """
    clock = FrozenClock(NOW)
    _, opp, _ = deliver(session, clock, message_id="<a@pjdick.com>")
    clock.advance(days=1)
    stray, _, _ = deliver(
        session,
        clock,
        subject="Benedum Hall Lab - Addendum 1",
        kind="addendum",
        addendum_label="Addendum 1",
        addendum_number=1,
        message_id="<b@pjdick.com>",
    )
    entry = curation.split_source(session, opportunity_id=opp.id, message_id=stray.id, clock=clock)
    fresh_id = entry.other_id
    # The parent picks up its own "Addendum 1" from a different message while the split stands.
    clock.advance(days=1)
    deliver(
        session,
        clock,
        subject="Benedum Hall Lab - Addendum 1 (resend)",
        kind="addendum",
        addendum_label="Addendum 1",
        addendum_number=1,
        in_reply_to="<a@pjdick.com>",
        references=("<a@pjdick.com>",),
        message_id="<c@pjdick.com>",
    )
    assert {a.opportunity_id for a in session.scalars(select(Addendum)).all()} == {
        opp.id,
        fresh_id,
    }

    curation.undo(session, merge_log_id=entry.id, clock=clock)

    assert session.get(Opportunity, fresh_id) is None
    rows = session.scalars(select(Addendum).where(Addendum.opportunity_id == opp.id)).all()
    assert len(rows) == 1 and rows[0].label == "Addendum 1"
    assert rows[0].copies == 2, "the twin was absorbed, not dropped"
