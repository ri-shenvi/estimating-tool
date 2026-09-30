from datetime import timedelta

from bidtriage.digest import (
    DigestItem,
    HealthLine,
    assemble,
    render_html,
    render_text,
    subject_line,
)


def _item(i: int, **kw):  # type: ignore[no-untyped-def]
    base = dict(
        opportunity_id=f"o{i}",
        project_name=f"Project {i}",
        gc_name="PJ Dick",
        gc_tier="A",
        score=80,
        band="bid",
        status="new",
        summary="Lighting and power.",
        why_positive=["higher education", "Tier A"],
        actions={"Bid": "http://x/a/1", "Pass": "http://x/a/2"},
        open_url=f"http://x/opportunities/o{i}",
    )
    base.update(kw)
    return DigestItem(**base)


def test_sections_and_no_duplicates(now):  # type: ignore[no-untyped-def]
    since = now - timedelta(days=1)
    items = [
        _item(
            1, first_seen_at=now - timedelta(hours=2), bid_due=now + timedelta(days=4)
        ),  # new AND needs decision -> decision only
        _item(2, first_seen_at=now - timedelta(hours=1), bid_due=now + timedelta(days=20)),  # new
        _item(
            3,
            first_seen_at=now - timedelta(days=5),
            status="bidding",
            bid_due=now + timedelta(days=3),
            changed_since_digest=True,
            change_summary="Due date moved Oct 16 → Oct 21",
        ),
        _item(
            4,
            first_seen_at=now - timedelta(days=5),
            status="passed",
            changed_since_digest=True,
            change_summary="size doubled",
        ),
        _item(
            5, first_seen_at=now - timedelta(hours=1), score=30, band="likely_pass", bid_due=None
        ),
        _item(6, first_seen_at=now - timedelta(hours=1), score=5, band="pass"),
        _item(
            7,
            first_seen_at=now - timedelta(days=5),
            status="undecided",
            prebid_at=now + timedelta(hours=3),
            prebid_mandatory=True,
            bid_due=now + timedelta(days=12),
        ),
    ]
    s = assemble(
        items=items,
        recipient_id="u1",
        recipient_name="Casey",
        recipient_role="chief",
        now=now,
        since=since,
        health=[HealthLine(name="estimating@", status="ok")],
    )
    ids_decision = {i.opportunity_id for i in s.needs_decision}
    ids_new = {i.opportunity_id for b in s.new_by_band.values() for i in b}
    assert "o1" in ids_decision and "o1" not in ids_new
    assert "o7" in ids_decision  # stale undecided, older than 3 days
    assert ids_new == {"o2", "o5", "o6"}
    assert [i.opportunity_id for i in s.changed] == ["o3"] and [
        i.opportunity_id for i in s.passed_changed
    ] == ["o4"]
    assert [i.opportunity_id for i in s.prebid_today] == ["o7"]
    assert {i.opportunity_id for i in s.due_this_week} == {"o3", "o7"} or "o3" in {
        i.opportunity_id for i in s.due_this_week
    }
    assert s.counts["new"] == 3 and s.counts["needs_decision"] == 2 and not s.quiet
    html = render_html(s)
    text = render_text(s)
    assert (
        "Due date unknown" in html and "Needs your decision (2)" in html and "Pre-bid TODAY" in html
    )
    assert "Due date moved Oct 16" in html and "Passed job changed materially" in html
    assert "<script" not in html and "NEEDS YOUR DECISION (2)" in text and "http://x/a/1" in text
    assert subject_line(s).startswith("Bids · Wed Sep 30 · 3 new (1 to bid)")


def test_quiet_morning(now):  # type: ignore[no-untyped-def]
    items = [
        _item(
            1,
            first_seen_at=now - timedelta(days=10),
            status="bidding",
            bid_due=now + timedelta(days=10),
            changed_since_digest=False,
        )
    ]
    s = assemble(
        items=items,
        recipient_id="u1",
        recipient_name="Casey",
        recipient_role="chief",
        now=now,
        since=now - timedelta(days=1),
    )
    assert s.quiet and s.in_progress_count == 1 and s.next_due.opportunity_id == "o1"
    assert "Quiet morning" in render_html(s) and "quiet morning" in subject_line(s)


def test_degraded_banner_and_subject(now):  # type: ignore[no-untyped-def]
    s = assemble(
        items=[],
        recipient_id="u1",
        recipient_name="Casey",
        recipient_role="chief",
        now=now,
        since=None,
        health=[HealthLine(name="estimating@", status="down", detail="last success 130 min ago")],
    )
    assert s.degraded and not s.quiet
    assert subject_line(s).startswith("⚠︎") and "last success 130 min ago" in render_html(s)


def test_html_escaping_and_truncation(now):  # type: ignore[no-untyped-def]
    items = [
        _item(
            1,
            first_seen_at=now,
            project_name="<script>alert(1)</script>" + "x" * 200,
            bid_due=now + timedelta(days=20),
        )
    ]
    s = assemble(
        items=items,
        recipient_id="u1",
        recipient_name="Casey",
        recipient_role="chief",
        now=now,
        since=None,
    )
    html = render_html(s)
    assert "<script>" not in html and "&lt;script&gt;" in html and "…" in html


def test_min_band_hides_low(now):  # type: ignore[no-untyped-def]
    items = [
        _item(1, first_seen_at=now, score=30, band="likely_pass", bid_due=now + timedelta(days=20)),
        _item(2, first_seen_at=now, score=10, band="pass"),
    ]
    s = assemble(
        items=items,
        recipient_id="u1",
        recipient_name="Sam",
        recipient_role="estimator",
        now=now,
        since=None,
        min_band="consider",
    )
    assert s.hidden_low_count == 2 and not s.new_by_band["likely_pass"]
    assert "2 low-fit invitations not shown" in render_html(s)


def test_readonly_has_no_action_links(now):  # type: ignore[no-untyped-def]
    items = [_item(1, first_seen_at=now, bid_due=now + timedelta(days=20))]
    s = assemble(
        items=items,
        recipient_id="p",
        recipient_name="President",
        recipient_role="readonly",
        now=now,
        since=None,
    )
    assert "http://x/a/1" not in render_html(s)


def test_consider_overflow(now):  # type: ignore[no-untyped-def]
    items = [
        _item(i, first_seen_at=now, score=50, band="consider", bid_due=now + timedelta(days=20))
        for i in range(15)
    ]
    s = assemble(
        items=items,
        recipient_id="u1",
        recipient_name="Casey",
        recipient_role="chief",
        now=now,
        since=None,
    )
    assert (
        len(s.new_by_band["consider"]) == 10 and s.consider_overflow == 5 and s.counts["new"] == 15
    )
