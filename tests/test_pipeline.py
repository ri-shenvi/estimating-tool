"""End-to-end on SQLite: fixtures -> ingest -> fake extraction -> resolve -> score -> digest items."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select

from bidtriage.core.clock import BUSINESS_TZ, FrozenClock
from bidtriage.core.models import Addendum, FieldHistory, Opportunity, RawMessage, Source, User
from bidtriage.extraction.fake import FakeExtractor
from bidtriage.gcs.seed import seed_gcs
from bidtriage.ingestion.eml import parse_eml
from bidtriage.worker import pipeline

HOME = (40.3462, -79.9482)


def _run(session, fixtures_dir, clock, name: str):  # type: ignore[no-untyped-def]
    src = session.scalar(select(Source)) or Source(kind="file", name="fx")
    session.add(src)
    session.flush()
    msg, is_new = pipeline.ingest_parsed(
        session,
        source_id=src.id,
        provider_message_id=name,
        parsed=parse_eml((fixtures_dir / name).read_bytes()),
        clock=clock,
    )
    if not is_new:
        return msg, None, None
    ext = pipeline.extract_message(
        session,
        msg,
        FakeExtractor(fixtures_dir),
        external_ref=name.removesuffix(".eml"),
        clock=clock,
    )
    opp, decision = pipeline.resolve_message(session, msg, ext, clock=clock)
    pipeline.score_opportunity(session, opp, now=clock.now(), home=HOME)
    return msg, opp, decision


def test_cross_channel_dedupe_and_date_change(session, fixtures_dir):  # type: ignore[no-untyped-def]
    clock = FrozenClock(datetime(2026, 9, 30, 6, 30, tzinfo=BUSINESS_TZ))
    seed_gcs(session)
    _, opp1, d1 = _run(session, fixtures_dir, clock, "bc_invite_benedum.eml")
    assert d1 == "new" and opp1.status == "new"
    assert opp1.canonical["bid_due"]["value"].startswith("2026-10-16T14:00")
    gc_name = opp1.canonical["gc_name"]["value"]
    assert gc_name == "PJ Dick" and opp1.gc_id is not None

    _, opp2, d2 = _run(session, fixtures_dir, clock, "gc_email_benedum.eml")
    assert d2 == "merge" and opp2.id == opp1.id
    assert "requested_by_name" in opp2.flags

    clock.advance(days=12)
    _, opp3, d3 = _run(session, fixtures_dir, clock, "date_change_benedum.eml")
    assert d3 == "merge" and opp3.id == opp1.id
    assert opp3.canonical["bid_due"]["value"].startswith("2026-10-21T14:00")
    hist = session.scalars(
        select(FieldHistory).where(
            FieldHistory.opportunity_id == opp1.id, FieldHistory.field == "bid_due"
        )
    ).all()
    assert len(hist) == 1 and hist[0].applied
    assert "moved Oct 16 → Oct 21" in (opp3.change_summary or "") and opp3.changed_since_digest
    assert (
        session.scalars(select(Addendum).where(Addendum.opportunity_id == opp1.id)).one().number
        == 2
    )
    assert "site_lighting" in opp3.canonical["scope_items"]
    assert session.query(Opportunity).count() == 1


def test_duplicate_copy_linked_not_recreated(session, fixtures_dir):  # type: ignore[no-untyped-def]
    """`copies` counts recipient paths, not re-reads of the same mailbox (SPEC-01 F3)."""
    clock = FrozenClock(datetime(2026, 9, 30, 6, 30, tzinfo=BUSINESS_TZ))
    _run(session, fixtures_dir, clock, "bc_invite_benedum.eml")
    parsed = parse_eml((fixtures_dir / "bc_invite_benedum.eml").read_bytes())
    src = session.scalar(select(Source))

    # Same mailbox, new provider id (an IMAP UIDVALIDITY reset): relinked, still one copy.
    msg, is_new = pipeline.ingest_parsed(
        session,
        source_id=src.id,
        provider_message_id="another-provider-id",
        parsed=parsed,
        clock=clock,
    )
    assert not is_new and msg.copies == 1 and session.query(RawMessage).count() == 1

    # A second connected mailbox that was CC'd is a second recipient path.
    other = Source(kind="imap", name="dana inbox", mailbox="dana@ferryelectric.com")
    session.add(other)
    session.flush()
    msg, is_new = pipeline.ingest_parsed(
        session,
        source_id=other.id,
        provider_message_id="INBOX:100:9",
        parsed=parsed,
        recipient_path=other.mailbox,
        clock=clock,
    )
    assert not is_new and msg.copies == 2 and session.query(RawMessage).count() == 1


def test_roofing_scores_near_zero(session, fixtures_dir):  # type: ignore[no-untyped-def]
    clock = FrozenClock(datetime(2026, 9, 30, 6, 30, tzinfo=BUSINESS_TZ))
    seed_gcs(session)
    msg, opp, _ = _run(session, fixtures_dir, clock, "roofing_itb.eml")
    assert msg.kind == "itb" and opp.canonical["trade_relevance"] == "none"
    from bidtriage.core.models import Score

    sc = session.scalars(select(Score).where(Score.opportunity_id == opp.id)).one()
    assert sc.score <= 5 and sc.band == "pass"


def test_forwarded_itb_resolves_gc_by_domain_and_scores_bid(session, fixtures_dir):  # type: ignore[no-untyped-def]
    clock = FrozenClock(datetime(2026, 9, 30, 6, 30, tzinfo=BUSINESS_TZ))
    seed_gcs(session)
    from bidtriage.core.models import GC

    mascaro = session.scalar(select(GC).where(GC.canonical_name == "Mascaro Construction"))
    mascaro.tier = "A"
    msg, opp, _ = _run(session, fixtures_dir, clock, "forward_inline_mascaro.eml")
    assert msg.from_addr == "bbuilder@mascaroconstruction.com" and opp.gc_id == mascaro.id
    from bidtriage.core.models import Score

    sc = session.scalars(select(Score).where(Score.opportunity_id == opp.id)).one()
    assert sc.band == "bid", sc.explanation


def test_digest_items_from_db(session, fixtures_dir):  # type: ignore[no-untyped-def]
    clock = FrozenClock(datetime(2026, 9, 30, 6, 30, tzinfo=BUSINESS_TZ))
    seed_gcs(session)
    u = User(email="casey@ferryelectric.com", name="Casey", role="chief")
    session.add(u)
    session.flush()
    _run(session, fixtures_dir, clock, "bc_invite_benedum.eml")
    _run(session, fixtures_dir, clock, "forward_inline_mascaro.eml")
    from bidtriage.digest import assemble, render_html
    from bidtriage.worker.digest_job import load_items

    items = load_items(session, recipient=u, base_url="http://x", secret_key="k", now=clock.now())
    assert len(items) == 2 and all(i.actions for i in items)
    snap = assemble(
        items=items,
        recipient_id=u.id,
        recipient_name=u.name,
        recipient_role=u.role,
        now=clock.now(),
        since=None,
    )
    html = render_html(snap)
    assert "Benedum Hall" in html and "AHN Wexford" in html and "mandatory" in html


def test_older_message_processed_later_does_not_move_date_back(session, fixtures_dir):  # type: ignore[no-untyped-def]
    """Backfill / late CC copies arrive out of order; a Sep 29 email must not undo an Oct 12 extension."""
    clock = FrozenClock(datetime(2026, 10, 13, 6, 30, tzinfo=BUSINESS_TZ))
    seed_gcs(session)
    _run(session, fixtures_dir, clock, "bc_invite_benedum.eml")
    _, opp, _ = _run(session, fixtures_dir, clock, "date_change_benedum.eml")
    assert opp.canonical["bid_due"]["value"].startswith("2026-10-21")
    _, opp2, d = _run(session, fixtures_dir, clock, "gc_email_benedum.eml")
    assert d == "merge" and opp2.id == opp.id
    assert opp2.canonical["bid_due"]["value"].startswith("2026-10-21")
    unapplied = session.scalars(
        select(FieldHistory).where(
            FieldHistory.opportunity_id == opp.id,
            FieldHistory.field == "bid_due",
            FieldHistory.applied.is_(False),
        )
    ).all()
    assert len(unapplied) == 1
