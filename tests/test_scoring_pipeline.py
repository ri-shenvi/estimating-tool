"""SPEC-04 against the database: rescoring triggers, profile versioning and the band-change note.

The engine itself is pure and tested in `test_scoring.py`. What is tested here is everything the
caller is responsible for: assembling the snapshot, deciding when a score is worth rewriting, and
telling the digest which opportunities an estimator should look at again.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from bidtriage.core.clock import FrozenClock
from bidtriage.core.models import (
    Opportunity,
    ProfileImmutableError,
    Score,
    ScoringProfile,
)
from bidtriage.scoring.profile import DEFAULT_PROFILE, Profile
from bidtriage.worker import handlers, pipeline

from .resolution_helpers import NOW, deliver

HOME = (40.3462, -79.9482)


def _opportunity(session, *, due: datetime | None, **canonical):  # type: ignore[no-untyped-def]
    c = {
        "project_type": "higher_education",
        "trade_relevance": "primary",
        "size_signals": {"stated_project_value": 12_000_000},
        "bid_type": "hard_bid",
        "sector": "private",
        "project_name": {"value": "Test Project"},
        **canonical,
    }
    if due is not None:
        c["bid_due"] = {"value": due.isoformat(), "confidence": 0.9}
    opp = Opportunity(
        status="new",
        canonical=c,
        first_seen_at=NOW,
        last_activity_at=NOW,
        flags=[],
    )
    session.add(opp)
    session.flush()
    return opp


def _scores(session, opp_id: str) -> list[Score]:
    return list(
        session.scalars(
            select(Score).where(Score.opportunity_id == opp_id).order_by(Score.computed_at)
        ).all()
    )


# ------------------------------------------------------------------ F8: rescoring triggers


def test_rescore_on_gc_resolution(session, pjdick):  # type: ignore[no-untyped-def]
    """A GC that resolves after the fact changes the tier, so it has to change the score."""
    pjdick.tier = "A"
    session.flush()
    clock = FrozenClock(NOW)
    # A platform invite whose signature block was an image: no GC name, and the sender domain
    # is BuildingConnected, which is never the GC.
    first_msg, opp, _ = deliver(
        session,
        clock,
        from_addr="notifications@buildingconnected.com",
        gc_name={"value": None, "confidence": 0.0},
        gc_contacts=[],
    )
    assert opp.gc_id is None
    first = pipeline.score_opportunity(session, opp, now=clock.now(), home=HOME)
    assert next(c for c in first.contributions if c.factor == "gc").value == 0.5

    # The GC's own email lands on the same thread a day later, carrying a domain the directory
    # knows. Nothing about the project changed; the thing we know about it did.
    clock.advance(days=1)
    _, same, decision = deliver(
        session,
        clock,
        from_addr="jdoe@pjdick.com",
        subject="RE: Benedum Hall Lab Renovation - Electrical ITB",
        in_reply_to=first_msg.internet_message_id,
    )
    assert decision == "merge" and same.id == opp.id
    assert opp.gc_id == pjdick.id

    second = pipeline.score_opportunity(session, opp, now=clock.now(), home=HOME)
    assert next(c for c in second.contributions if c.factor == "gc").value == 1.0
    assert second.score > first.score
    assert len(_scores(session, opp.id)) == 2


def _timing(row: Score) -> float:
    return next(c for c in row.explanation["contributions"] if c["factor"] == "timing")["value"]


def test_timing_boundary_not_a_change(session):  # type: ignore[no-untyped-def]
    """The calendar advancing past a timing boundary is not news for the digest."""
    opp = _opportunity(session, due=NOW + timedelta(days=10))
    pipeline.rescore(session, [opp], now=NOW, home=HOME)
    before = _scores(session, opp.id)[-1]
    assert _timing(before) == pytest.approx(1.0)

    # Three days closer and still inside the 7-21 day band: the factor has not moved.
    pipeline.rescore(session, [opp], now=NOW + timedelta(days=2), home=HOME)
    assert _timing(_scores(session, opp.id)[-1]) == pytest.approx(1.0)

    # An hour past the boundary, and only then, it drops.
    pipeline.rescore(session, [opp], now=NOW + timedelta(days=3, hours=1), home=HOME)
    after = _scores(session, opp.id)[-1]
    assert _timing(after) == pytest.approx(0.6)
    assert after.score < before.score
    # The factor moved, and the digest is told nothing: no estimator action follows from it.
    assert opp.changed_since_digest is False and opp.change_summary is None


def test_rescore_is_idempotent(session):  # type: ignore[no-untyped-def]
    """ADR-005: rescoring the same facts under the same profile writes no second row."""
    opp = _opportunity(session, due=NOW + timedelta(days=14, hours=12))
    pipeline.rescore(session, [opp], now=NOW, home=HOME)
    pipeline.rescore(session, [opp], now=NOW, home=HOME)
    pipeline.rescore(session, [opp], now=NOW + timedelta(minutes=5), home=HOME)
    assert len(_scores(session, opp.id)) == 1

    # A day later the explanation itself reads differently ("due in 13 days"), and that is a
    # real change to what the digest would print.
    pipeline.rescore(session, [opp], now=NOW + timedelta(days=1), home=HOME)
    assert len(_scores(session, opp.id)) == 2


def test_nightly_rescore_does_not_annotate_band_changes(session, monkeypatch):  # type: ignore[no-untyped-def]
    """F8: only a profile activation puts "Rescored under profile vN" on the digest."""
    calls: list[str | None] = []

    def fake_rescore(_session, _opps, **kw):  # type: ignore[no-untyped-def]
        calls.append(kw.get("note_band_change"))
        return 0

    monkeypatch.setattr(pipeline, "rescore", fake_rescore)
    ctx = handlers.Context(None)
    handlers.rescore_all(session, {}, ctx)
    handlers.rescore_all(session, {"profile_version": 7}, ctx)
    assert calls == [None, "Rescored under profile v7"]


def test_congestion_counts_the_week_not_the_board(session):  # type: ignore[no-untyped-def]
    """F1: three other bids due that week multiply the timing factor by 0.6."""
    due = NOW + timedelta(days=14)
    subject = _opportunity(session, due=due)
    alone = pipeline.score_opportunity(session, subject, now=NOW, home=HOME)
    assert next(c for c in alone.contributions if c.factor == "timing").value == pytest.approx(1.0)

    for offset in (0, 1, 2):
        other = _opportunity(session, due=due + timedelta(days=offset))
        other.status = "bidding"
    # One more well outside the week, which must not count.
    far = _opportunity(session, due=due + timedelta(days=30))
    far.status = "bidding"
    session.flush()

    crowded = pipeline.score_opportunity(session, subject, now=NOW, home=HOME)
    timing = next(c for c in crowded.contributions if c.factor == "timing")
    assert timing.value == pytest.approx(0.6) and "3 other bids due that week" in timing.reason


# ------------------------------------------------------------------ F7: profile versions


def _trimmed_higher_ed() -> Profile:
    """The chief estimator demotes higher ed and healthcare becomes the top type.

    Something has to stay at 1.0: the table is a ranking relative to Ferry's best work, so a
    profile with no best type is a profile that has lost its scale (SPEC-04 F7 validation).
    """
    table = dict(DEFAULT_PROFILE.type_table)
    table["higher_education"] = 0.8
    table["healthcare"] = 1.0
    return Profile(type_table=table)


def _save_profile(session, profile: Profile, *, note: str) -> ScoringProfile:
    row = ScoringProfile(
        json=profile.model_dump(mode="json"),
        note=note,
        active=False,
        created_at=datetime.now(tz=UTC),
    )
    session.add(row)
    session.flush()
    return row


def test_activating_a_profile_rescores_and_flags_band_changes(session):  # type: ignore[no-untyped-def]
    """The acceptance criterion: higher education 1.0 -> 0.8, activate, everything rescored."""
    borderline = _opportunity(
        session,
        due=NOW + timedelta(days=14),
        size_signals={"stated_electrical_value": 194_000},
    )
    steady = _opportunity(
        session,
        due=NOW + timedelta(days=14),
        project_type="retail_restaurant",
        size_signals={"stated_project_value": 800_000},
    )
    pipeline.rescore(session, [borderline, steady], now=NOW, home=HOME)
    v1 = session.scalar(select(ScoringProfile).where(ScoringProfile.active.is_(True)))
    assert v1 is not None
    assert _scores(session, borderline.id)[-1].band == "bid"

    v2 = _save_profile(session, _trimmed_higher_ed(), note="trim higher ed")
    v1.active = False
    v2.active = True
    session.flush()

    handlers.rescore_all(session, {"profile_version": v2.version}, handlers.Context(None))

    for opp in (borderline, steady):
        latest = _scores(session, opp.id)[-1]
        assert latest.profile_version == v2.version

    assert _scores(session, borderline.id)[-1].band == "consider"
    assert borderline.changed_since_digest is True
    assert borderline.change_summary == f"Rescored under profile v{v2.version}"
    # The one whose band held is not reported as changed.
    assert steady.changed_since_digest is False


def test_profile_immutable(session):  # type: ignore[no-untyped-def]
    """Versions are immutable and are never deleted; a score has to stay explainable."""
    opp = _opportunity(session, due=NOW + timedelta(days=14))
    pipeline.score_opportunity(session, opp, now=NOW, home=HOME)
    session.commit()
    row = session.scalar(select(ScoringProfile))
    assert row is not None and session.scalar(select(func.count()).select_from(Score)) == 1

    session.delete(row)
    with pytest.raises(ProfileImmutableError):
        session.flush()
    session.rollback()

    row = session.scalar(select(ScoringProfile))
    assert row is not None
    row.json = {**row.json, "name": "rewritten"}
    with pytest.raises(ProfileImmutableError):
        session.flush()
    session.rollback()

    # Activation is the one change a version is allowed.
    row = session.scalar(select(ScoringProfile))
    assert row is not None
    row.active = not row.active
    session.flush()


def test_preview_reports_band_changes_without_writing(session):  # type: ignore[no-untyped-def]
    """F7: the editor's preview rescores the last 30 days and touches nothing."""
    opp = _opportunity(
        session,
        due=NOW + timedelta(days=14),
        size_signals={"stated_electrical_value": 194_000},
    )
    pipeline.rescore(session, [opp], now=NOW, home=HOME)
    written = session.scalar(select(func.count()).select_from(Score))

    preview = pipeline.preview_profile(session, _trimmed_higher_ed(), now=NOW, home=HOME)

    assert preview.scored == 1
    assert [c.before_band for c in preview.changes] == ["bid"]
    assert [c.after_band for c in preview.changes] == ["consider"]
    assert session.scalar(select(func.count()).select_from(Score)) == written


def test_preview_surfaces_profile_warnings(session):  # type: ignore[no-untyped-def]
    table = dict(DEFAULT_PROFILE.type_table)
    del table["healthcare"]
    preview = pipeline.preview_profile(session, Profile(type_table=table), now=NOW, home=HOME)
    assert any("healthcare" in w for w in preview.warnings)


# ------------------------------------------------------------------ performance


def test_rescore_performance(session):  # type: ignore[no-untyped-def]
    """F8: the nightly pass over 5,000 opportunities finishes well inside the digest window."""
    due = NOW + timedelta(days=14)
    opps = []
    for i in range(5_000):
        opp = Opportunity(
            status="bidding" if i % 3 == 0 else "new",
            canonical={
                "project_type": "higher_education",
                "size_signals": {"stated_project_value": 1_000_000 + i},
                "bid_due": {"value": (due + timedelta(days=i % 40)).isoformat()},
                "project_name": {"value": f"Project {i}"},
            },
            first_seen_at=NOW,
            last_activity_at=NOW,
            flags=[],
        )
        session.add(opp)
        opps.append(opp)
    session.flush()

    started = time.perf_counter()
    scored = pipeline.rescore(session, opps, now=NOW, home=HOME)
    elapsed = time.perf_counter() - started

    assert scored == 5_000
    assert session.scalar(select(func.count()).select_from(Score)) == 5_000
    assert elapsed < 60, f"rescoring 5,000 opportunities took {elapsed:.1f}s"


def test_snapshot_reads_the_size_range_and_due_confidence(session):  # type: ignore[no-untyped-def]
    """The snapshot carries what F2 and F3 need out of the canonical record."""
    opp = _opportunity(
        session,
        due=NOW + timedelta(days=14),
        size_signals={
            "stated_project_value": 1_350_000,
            "source": "budget of $1.2 - 1.5 million",
        },
    )
    o, _, _ = pipeline.snapshot_for(session, opp, now=NOW, home=HOME)
    assert o.size_source == "budget of $1.2 - 1.5 million"
    assert o.bid_due_confidence == 0.9
    result = pipeline.score_opportunity(session, opp, now=NOW, home=HOME)
    assert "$1.2 - 1.5 million" in result.size_estimate.reason


def test_owner_direct_snapshot(session):  # type: ignore[no-untyped-def]
    """Nobody named a GC and the owner did the inviting: scored as a Separations Act prime."""
    opp = _opportunity(
        session,
        due=NOW + timedelta(days=14),
        owner_name={"value": "Allegheny County"},
    )
    o, _, _ = pipeline.snapshot_for(session, opp, now=NOW, home=HOME)
    assert o.gc_is_owner_direct is True
    result = pipeline.score_opportunity(session, opp, now=NOW, home=HOME)
    gc = next(c for c in result.contributions if c.factor == "gc")
    assert gc.value == 0.6 and gc.reason == "owner-direct solicitation"


# ------------------------------------------------------------------ F6: what the digest says


def test_digest_why_line_never_contradicts_itself(session):  # type: ignore[no-untyped-def]
    """The reported defect: every row read "size unknown (+) ... size unknown (-)"."""
    from bidtriage.core.models import User
    from bidtriage.digest import assemble, render_text
    from bidtriage.worker.digest_job import load_items

    user = User(email="casey@ferryelectric.com", name="Casey", role="chief")
    session.add(user)
    opp = _opportunity(
        session,
        due=NOW + timedelta(days=60),
        project_type="government_civic",
        size_signals={},
        project_name={"value": "Borough Annex"},
    )
    session.flush()
    pipeline.rescore(session, [opp], now=NOW, home=None)

    items = load_items(session, recipient=user, base_url="http://x", secret_key="k", now=NOW)
    item = next(i for i in items if i.opportunity_id == opp.id)

    assert "size unknown" not in item.why_positive
    assert not set(item.why_positive) & set(item.why_negative)
    assert item.why_positive  # government civic and a comfortable deadline both genuinely help

    text = render_text(
        assemble(
            items=items,
            recipient_id=user.id,
            recipient_name=user.name,
            recipient_role=user.role,
            now=NOW,
            since=None,
        )
    )
    assert "size unknown (+)" not in text


def test_digest_why_line_is_omitted_when_nothing_is_known(session):  # type: ignore[no-untyped-def]
    """F6: fewer than three factors clear the bar, so fewer than three are rendered — or none."""
    from bidtriage.core.models import User
    from bidtriage.worker.digest_job import load_items

    user = User(email="casey@ferryelectric.com", name="Casey", role="chief")
    session.add(user)
    opp = _opportunity(
        session, due=None, project_type="unknown", size_signals={}, bid_type="unknown"
    )
    session.flush()
    pipeline.rescore(session, [opp], now=NOW, home=None)

    item = next(
        i
        for i in load_items(session, recipient=user, base_url="http://x", secret_key="k", now=NOW)
        if i.opportunity_id == opp.id
    )
    assert item.why_positive == [] and item.why_negative == []
