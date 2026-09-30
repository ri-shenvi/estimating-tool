from datetime import timedelta

import pytest

from bidtriage.core.jobs import claim, complete, enqueue, fail
from bidtriage.core.models import Decision, Opportunity, Outcome, User
from bidtriage.decisions.actions import apply_action
from bidtriage.decisions.state import InvalidTransitionError


def test_enqueue_idempotent_and_claim(session, clock):  # type: ignore[no-untyped-def]
    assert enqueue(session, "x", "k1", {"a": 1}, clock=clock) is not None
    assert enqueue(session, "x", "k1", {"a": 2}, clock=clock) is None
    job = claim(session, clock=clock)
    assert job is not None and job.attempts == 1 and job.status == "leased"
    assert claim(session, clock=clock) is None  # leased
    complete(session, job, clock=clock)
    assert job.status == "done"


def test_fail_backoff_then_failed(session, clock):  # type: ignore[no-untyped-def]
    enqueue(session, "x", "k2", max_attempts=2, clock=clock)
    job = claim(session, clock=clock)
    fail(session, job, "boom", clock=clock)
    assert job.status == "pending" and job.run_at > clock.now()
    assert claim(session, clock=clock) is None
    clock.advance(minutes=5)
    job = claim(session, clock=clock)
    fail(session, job, "boom again", clock=clock)
    assert job.status == "failed"


def test_lease_expiry_reclaims(session, clock):  # type: ignore[no-untyped-def]
    enqueue(session, "x", "k3", clock=clock)
    job = claim(session, lease=timedelta(minutes=1), clock=clock)
    clock.advance(minutes=2)
    again = claim(session, clock=clock)
    assert again is not None and again.id == job.id and again.attempts == 2


def _opp(session, clock, **canon):  # type: ignore[no-untyped-def]
    if session.get(User, "u1") is None:
        session.add(User(id="u1", email="u1@example.com", name="U1", role="estimator"))
        session.flush()
    o = Opportunity(
        status="new",
        canonical={"project_name": {"value": "P"}, **canon},
        first_seen_at=clock.now(),
        last_activity_at=clock.now(),
    )
    session.add(o)
    session.flush()
    return o


def test_bid_assigns_actor_and_audits(session, clock):  # type: ignore[no-untyped-def]
    o = _opp(session, clock)
    res = apply_action(
        session, o, "bid", actor_user_id="u1", actor_role="estimator", channel="digest", clock=clock
    )
    assert (
        o.status == "bidding" and o.assignee_user_id == "u1" and res["after"]["status"] == "bidding"
    )
    assert session.query(Decision).count() == 1


def test_pass_reason_validation(session, clock):  # type: ignore[no-untyped-def]
    o = _opp(session, clock)
    with pytest.raises(ValueError):
        apply_action(
            session,
            o,
            "pass",
            actor_user_id="u1",
            actor_role="estimator",
            channel="review",
            reason="because",
            clock=clock,
        )
    apply_action(
        session,
        o,
        "pass",
        actor_user_id="u1",
        actor_role="estimator",
        channel="review",
        reason="too_far",
        clock=clock,
    )
    assert o.status == "passed"


def test_snooze_overridden_by_due_date(session, clock):  # type: ignore[no-untyped-def]
    due = clock.now() + timedelta(days=5)
    o = _opp(session, clock, bid_due={"value": due.isoformat()})
    res = apply_action(
        session,
        o,
        "snooze",
        actor_user_id="u1",
        actor_role="estimator",
        channel="digest",
        payload={"days": 7},
        clock=clock,
    )
    assert (
        o.status == "snoozed"
        and o.snooze_until == due - timedelta(days=2)
        and res["after"]["snooze_until"] is not None
    )
    d = session.query(Decision).one()
    assert d.payload["snooze_overridden"] is True


def test_invalid_transition_and_outcome(session, clock):  # type: ignore[no-untyped-def]
    o = _opp(session, clock)
    with pytest.raises(InvalidTransitionError):
        apply_action(
            session, o, "won", actor_user_id="u1", actor_role="chief", channel="review", clock=clock
        )
    apply_action(
        session, o, "bid", actor_user_id="u1", actor_role="chief", channel="review", clock=clock
    )
    apply_action(
        session,
        o,
        "submit",
        actor_user_id="u1",
        actor_role="chief",
        channel="review",
        payload={"price": 1_234_000},
        clock=clock,
    )
    apply_action(
        session, o, "won", actor_user_id="u1", actor_role="chief", channel="review", clock=clock
    )
    assert o.status == "won" and session.query(Outcome).count() == 2
