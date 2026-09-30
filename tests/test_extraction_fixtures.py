"""The SPEC-02 edge-case table, one test per row.

Each row is an `.eml` in `tests/fixtures/messages` plus the `.expected.json` an estimator would
have written. The fixture carries the model's half of the answer; these tests assert the half the
post-processor owns — the dates, the flags, the sources, the things the model is not allowed to have
the last word on.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from bidtriage.core.clock import BUSINESS_TZ
from bidtriage.extraction.fake import FakeExtractor
from bidtriage.extraction.prefilter import obviously_not_bid
from bidtriage.extraction.protocol import AttachmentText, ExtractionInput
from bidtriage.extraction.schema import (
    KIND_REVIEW_CONFIDENCE,
    BidType,
    ExtractedOpportunity,
    Flag,
    Kind,
    ScopeItem,
    Sector,
    TradeRelevance,
)
from bidtriage.ingestion.attachments import extract_text
from bidtriage.ingestion.eml import parse_eml


def _input(fixtures_dir: Path, stem: str) -> ExtractionInput:
    parsed = parse_eml((fixtures_dir / f"{stem}.eml").read_bytes())
    attachments = []
    for a in parsed.attachments:
        text = extract_text(a.filename, a.mime, a.data)
        attachments.append(
            AttachmentText(
                filename=a.filename, text=text.text or "", large_document=text.large_document
            )
        )
    assert parsed.sent_at is not None
    return ExtractionInput(
        message_id=stem,
        subject=parsed.subject,
        from_addr=parsed.from_addr,
        from_name=parsed.from_name,
        to=parsed.to,
        sent_at=parsed.sent_at,
        body=parsed.body_trimmed,
        attachments=attachments,
        external_ref=stem,
    )


@pytest.fixture
def record(fixtures_dir):  # type: ignore[no-untyped-def]
    """Post-processed record for a fixture, exactly as the pipeline would produce it."""

    def _record(stem: str) -> ExtractedOpportunity:
        return FakeExtractor(fixtures_dir).extract(_input(fixtures_dir, stem))

    return _record


def _et(y, m, d, hh=0, mm=0):  # type: ignore[no-untyped-def]
    return datetime(y, m, d, hh, mm, tzinfo=BUSINESS_TZ)


# ------------------------------------------------------------------ dates


def test_date_year_rollover(record):  # type: ignore[no-untyped-def]
    """Sent in December, due "January 8": next year's January 8."""
    x = record("date_year_rollover")
    assert x.bid_due.value == _et(2027, 1, 8, 14) and x.bid_due.time_known


def test_date_relative_weekday(record):  # type: ignore[no-untyped-def]
    x = record("date_relative_weekday")
    assert x.bid_due.value == _et(2026, 10, 2, 12) and x.bid_due.value.strftime("%A") == "Friday"


def test_date_conflict_gc_owner(record):  # type: ignore[no-untyped-def]
    x = record("date_conflict_gc_owner")
    assert x.bid_due.value == _et(2026, 10, 14, 10)
    assert Flag.conflicting_dates in x.flags
    assert "Oct 14 at 10 AM" in x.bid_due.source and "Oct 16 at 2 PM" in x.bid_due.source


def test_date_already_past(record):  # type: ignore[no-untyped-def]
    x = record("date_already_past")
    assert x.bid_due.value == _et(2026, 10, 6)
    assert Flag.past_due_at_receipt in x.flags and x.bid_due.confidence <= 0.3


def test_date_foreign_timezone(record):  # type: ignore[no-untyped-def]
    """Stored as Central with the zone recorded; the digest renders it in Eastern."""
    x = record("date_foreign_timezone")
    assert x.bid_due.timezone == "America/Chicago"
    assert x.bid_due.value.utcoffset().total_seconds() == -5 * 3600
    assert x.bid_due.value.astimezone(BUSINESS_TZ) == _et(2026, 10, 20, 15)


def test_typo_time(record):  # type: ignore[no-untyped-def]
    x = record("typo_time")
    assert x.bid_due.value == _et(2026, 10, 19, 14) and x.bid_due.confidence <= 0.8


# ------------------------------------------------------------------ pre-bid and missing dates


def test_prebid_optional(record):  # type: ignore[no-untyped-def]
    x = record("prebid_optional")
    assert x.prebid.mandatory is False and Flag.mandatory_prebid not in x.flags
    assert x.prebid.value == _et(2026, 10, 8, 9)


def test_mandatory_prebid_from_a_pdf_letter(record):  # type: ignore[no-untyped-def]
    x = record("mandatory_prebid_pdf")
    assert x.prebid.mandatory is True and Flag.mandatory_prebid in x.flags
    assert x.prebid.value == _et(2026, 10, 7, 10)
    assert x.prebid.source_location.startswith("attachment:ITB-Letter-Hillman.pdf")


def test_no_due_date(record):  # type: ignore[no-untyped-def]
    x = record("no_due_date")
    assert x.bid_due.value is None and x.bid_due.confidence == 0.0
    assert x.prebid.value == _et(2026, 10, 9, 10) and x.kind == Kind.prebid_notice


# ------------------------------------------------------------------ size


def test_size_project_value_only(record):  # type: ignore[no-untyped-def]
    x = record("size_project_value_only")
    assert x.size_signals.stated_project_value == 12_000_000
    assert x.size_signals.stated_electrical_value is None


def test_size_electrical_value(record):  # type: ignore[no-untyped-def]
    x = record("size_electrical_value")
    assert x.size_signals.stated_electrical_value == 850_000


def test_size_sf_stories(record):  # type: ignore[no-untyped-def]
    x = record("size_sf_stories")
    assert x.size_signals.square_feet == 45_000 and x.size_signals.stories == 3


def test_size_range(record):  # type: ignore[no-untyped-def]
    x = record("size_range")
    assert x.size_signals.stated_electrical_value == 1_350_000
    assert "$1.2 - 1.5 million" in x.size_signals.source


# ------------------------------------------------------------------ scope


def test_scope_with_exclusion(record):  # type: ignore[no-untyped-def]
    x = record("scope_with_exclusion")
    assert ScopeItem.fire_alarm in x.scope_items
    assert ScopeItem.structured_cabling in x.scope_items
    assert ScopeItem.security_access_control not in x.scope_items
    assert "Security by Owner" in x.exclusions_text


def test_design_build(record):  # type: ignore[no-untyped-def]
    x = record("design_build")
    assert x.bid_type == BidType.design_build
    assert ScopeItem.design_build_engineering in x.scope_items


# ------------------------------------------------------------------ classification


def test_procore_table_labels_map_to_the_right_fields(record):  # type: ignore[no-untyped-def]
    x = record("procore_table")
    assert x.bid_due.value == _et(2026, 10, 23, 14)
    assert x.rfi_deadline.value == _et(2026, 10, 15)
    assert x.intent_due.value == _et(2026, 10, 5)
    assert x.prebid.value == _et(2026, 10, 12, 9)
    assert x.delivery_channel.value == "procore"


def test_date_change_is_not_an_addendum(record):  # type: ignore[no-untyped-def]
    x = record("date_change_procore")
    assert x.kind == Kind.date_change
    assert x.bid_due.value == _et(2026, 10, 21, 14)
    assert "extended to October 21 at 2:00 PM" in x.changes_described


def test_addendum_number_from_the_subject(record):  # type: ignore[no-untyped-def]
    x = record("addendum_subject")
    assert x.kind == Kind.addendum and x.addendum_number == 2


def test_forwarded_thread_anchors_on_the_original(record):  # type: ignore[no-untyped-def]
    """The forwarder's note and the mechanical contractor's reply are not facts about the job."""
    x = record("forwarded_thread")
    assert x.gc_name.value == "Shannon Construction"
    assert [c.email for c in x.gc_contacts] == ["kshannon@shannonconstruction.com"]
    assert x.bid_due.value == _et(2026, 10, 20, 14)


def test_cm_and_owner_are_separate(record):  # type: ignore[no-untyped-def]
    x = record("cm_and_owner")
    assert x.gc_name.value == "Turner Construction" and x.owner_name.value == "UPMC"


def test_itb_vs_rfb(record):  # type: ignore[no-untyped-def]
    """Titled "Invitation to Bid", but the body asks for budget pricing, so it is an RFB."""
    x = record("itb_vs_rfb")
    assert x.kind == Kind.rfb and x.bid_type == BidType.budget


def test_cancelled(record):  # type: ignore[no-untyped-def]
    x = record("cancelled")
    assert x.kind == Kind.award and x.changes_described == "cancelled"


def test_public_advertisement(record):  # type: ignore[no-untyped-def]
    x = record("public_advertisement")
    assert x.sector == Sector.public and x.bid_type == BidType.hard_bid
    assert {Flag.prevailing_wage, Flag.sealed_bid} <= set(x.flags)
    assert x.trade_relevance == TradeRelevance.primary


def test_roofing_itb_is_still_an_itb(record):  # type: ignore[no-untyped-def]
    x = record("roofing_itb")
    assert x.kind == Kind.itb and x.trade_relevance == TradeRelevance.none


def test_vendor_newsletter_never_reaches_the_model(record, fixtures_dir):  # type: ignore[no-untyped-def]
    assert obviously_not_bid(_input(fixtures_dir, "vendor_newsletter"))
    assert record("vendor_newsletter").kind == Kind.not_bid


def test_ocr_garbled_survives_with_low_confidence(record):  # type: ignore[no-untyped-def]
    x = record("ocr_garbled")
    assert x.kind == Kind.itb and x.kind_confidence < KIND_REVIEW_CONFIDENCE
    assert x.bid_due.value == _et(2026, 10, 22, 14)
    assert x.project_name.confidence <= 0.6 and x.location.confidence <= 0.6


# ------------------------------------------------------------------ attachments and sources


def test_attachment_only_message_sources_every_field_to_the_attachment(record):  # type: ignore[no-untyped-def]
    x = record("attachment_only")
    located = [
        f.source_location
        for f in (x.project_name, x.gc_name, x.owner_name, x.location, x.bid_due, x.size_signals)
    ]
    assert located and all(loc and loc.startswith("attachment:") for loc in located)
    assert x.bid_due.value == _et(2026, 10, 26, 15)


def test_huge_spec_book_is_flagged_as_truncated(record):  # type: ignore[no-untyped-def]
    x = record("huge_spec_book")
    assert Flag.attachment_truncated in x.flags
    assert x.bid_due.value == _et(2026, 10, 29, 14)


def test_empty_summary_is_synthesized(record):  # type: ignore[no-untyped-def]
    x = record("empty_summary")
    assert Flag.summary_synthesized in x.flags
    assert "Frick Fine Arts" in x.summary and "lighting" in x.summary


def test_every_summary_fits_the_digest(record, fixtures_dir):  # type: ignore[no-untyped-def]
    """SPEC-02 F2 caps the summary at 60 words; the digest layout depends on it."""
    for path in sorted(fixtures_dir.glob("*.expected.json")):
        x = record(path.name.removesuffix(".expected.json"))
        assert 0 < len(x.summary.split()) <= 60, path.name


def test_every_source_excerpt_is_within_the_cap(record, fixtures_dir):  # type: ignore[no-untyped-def]
    for path in sorted(fixtures_dir.glob("*.expected.json")):
        x = record(path.name.removesuffix(".expected.json"))
        for field in (x.project_name, x.gc_name, x.owner_name, x.location, x.bid_due, x.prebid):
            assert field.source is None or len(field.source) <= 200, path.name


# ------------------------------------------------------------------ platform invitations


def test_buildingconnected_invitation(record):  # type: ignore[no-untyped-def]
    """The platform delivered it; the GC sent it. Neither the name nor the address may be confused."""
    x = record("bc_invite_benedum")
    assert x.gc_name.value == "PJ Dick"
    assert x.delivery_channel.value == "buildingconnected"
    assert [c.name for c in x.gc_contacts] == ["Jane Doe"]
    assert not any("buildingconnected.com" in (c.email or "") for c in x.gc_contacts)


def test_gc_email_due_date_keeps_the_quoted_sentence(record):  # type: ignore[no-untyped-def]
    x = record("gc_email_benedum")
    assert x.bid_due.value == _et(2026, 10, 16, 14) and x.bid_due.time_known
    assert x.bid_due.value.isoformat() == "2026-10-16T14:00:00-04:00"
    assert "Bids are due Thursday, October 16th at 2:00 PM." in x.bid_due.source
