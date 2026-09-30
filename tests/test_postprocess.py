from datetime import datetime

from bidtriage.core.clock import BUSINESS_TZ
from bidtriage.extraction.postprocess import parse_local, postprocess
from bidtriage.extraction.schema import (
    Contact,
    Flag,
    LLMDateTimeField,
    LLMExtraction,
    LLMPrebid,
    StrField,
)


def _llm(**kw):  # type: ignore[no-untyped-def]
    base = dict(kind="itb", kind_confidence=0.9, summary="A project.")
    base.update(kw)
    return LLMExtraction.model_validate(base)


def test_year_rollover():  # type: ignore[no-untyped-def]
    anchor = datetime(2026, 12, 20, tzinfo=BUSINESS_TZ)
    dt, known = parse_local("1/8", None, anchor)
    assert dt is not None and dt.year == 2027 and dt.month == 1 and not known


def test_date_only_time_unknown():  # type: ignore[no-untyped-def]
    x = postprocess(
        _llm(bid_due=LLMDateTimeField(value="2026-10-16", confidence=0.8)),
        datetime(2026, 9, 30, tzinfo=BUSINESS_TZ),
    )
    assert (
        x.bid_due.value == datetime(2026, 10, 16, tzinfo=BUSINESS_TZ) and not x.bid_due.time_known
    )


def test_full_datetime_eastern():  # type: ignore[no-untyped-def]
    x = postprocess(
        _llm(
            bid_due=LLMDateTimeField(value="2026-10-16T14:00:00", time_known=True, confidence=0.98)
        ),
        datetime(2026, 9, 30, tzinfo=BUSINESS_TZ),
    )
    assert x.bid_due.value.isoformat() == "2026-10-16T14:00:00-04:00" and x.bid_due.time_known


def test_foreign_timezone_preserved():  # type: ignore[no-untyped-def]
    x = postprocess(
        _llm(
            bid_due=LLMDateTimeField(
                value="2026-10-16T14:00:00",
                timezone="America/Chicago",
                time_known=True,
                confidence=0.9,
            )
        ),
        datetime(2026, 9, 30, tzinfo=BUSINESS_TZ),
    )
    assert x.bid_due.value.utcoffset().total_seconds() == -5 * 3600


def test_past_due_flag_and_confidence_cap():  # type: ignore[no-untyped-def]
    x = postprocess(
        _llm(bid_due=LLMDateTimeField(value="2026-10-06", confidence=0.9)),
        datetime(2026, 10, 9, tzinfo=BUSINESS_TZ),
    )
    assert Flag.past_due_at_receipt in x.flags and x.bid_due.confidence <= 0.3


def test_mandatory_prebid_sets_flag():  # type: ignore[no-untyped-def]
    x = postprocess(
        _llm(prebid=LLMPrebid(value="2026-10-07T10:00:00", mandatory=True, confidence=0.9)),
        datetime(2026, 9, 30, tzinfo=BUSINESS_TZ),
    )
    assert Flag.mandatory_prebid in x.flags and x.prebid.value.hour == 10


def test_empty_summary_synthesized():  # type: ignore[no-untyped-def]
    x = postprocess(
        _llm(summary="", project_type="healthcare", scope_items=["lighting"]),
        datetime(2026, 9, 30, tzinfo=BUSINESS_TZ),
    )
    assert Flag.summary_synthesized in x.flags and "healthcare" in x.summary


def test_platform_contacts_removed():  # type: ignore[no-untyped-def]
    x = postprocess(
        _llm(
            gc_contacts=[
                Contact(name="BC", email="team@buildingconnected.com"),
                Contact(name="Jane", email="jane@pjdick.com"),
            ]
        ),
        datetime(2026, 9, 30, tzinfo=BUSINESS_TZ),
    )
    assert [c.email for c in x.gc_contacts] == ["jane@pjdick.com"]


def test_summary_truncated_to_60_words():  # type: ignore[no-untyped-def]
    x = postprocess(
        _llm(summary=" ".join(["word"] * 80)), datetime(2026, 9, 30, tzinfo=BUSINESS_TZ)
    )
    assert len(x.summary.split()) == 60


def test_garbage_date_is_none():  # type: ignore[no-untyped-def]
    x = postprocess(
        _llm(bid_due=LLMDateTimeField(value="TBD", confidence=0.5)),
        datetime(2026, 9, 30, tzinfo=BUSINESS_TZ),
    )
    assert x.bid_due.value is None and x.bid_due.confidence == 0.0


def test_relative_weekday_resolves_to_first_such_day_on_or_after_sent():  # type: ignore[no-untyped-def]
    # Sent Tuesday 29 Sep 2026; "Friday at noon" is 2 October.
    x = postprocess(
        _llm(
            bid_due=LLMDateTimeField(value="weekday:friday@12:00", time_known=True, confidence=0.85)
        ),
        datetime(2026, 9, 29, 14, 22, tzinfo=BUSINESS_TZ),
    )
    assert x.bid_due.value == datetime(2026, 10, 2, 12, 0, tzinfo=BUSINESS_TZ)
    assert x.bid_due.time_known


def test_relative_weekday_on_the_same_weekday_stays_today():  # type: ignore[no-untyped-def]
    x = postprocess(
        _llm(bid_due=LLMDateTimeField(value="weekday:fri@12:00", time_known=True, confidence=0.8)),
        datetime(2026, 10, 2, 8, 0, tzinfo=BUSINESS_TZ),
    )
    assert x.bid_due.value == datetime(2026, 10, 2, 12, 0, tzinfo=BUSINESS_TZ)


def test_weekday_without_a_time_is_not_a_known_time():  # type: ignore[no-untyped-def]
    x = postprocess(
        _llm(bid_due=LLMDateTimeField(value="weekday:thursday", confidence=0.7)),
        datetime(2026, 9, 29, tzinfo=BUSINESS_TZ),
    )
    assert x.bid_due.value == datetime(2026, 10, 1, tzinfo=BUSINESS_TZ) and not x.bid_due.time_known


def test_unknown_weekday_is_no_date():  # type: ignore[no-untyped-def]
    x = postprocess(
        _llm(bid_due=LLMDateTimeField(value="weekday:someday@09:00", confidence=0.6)),
        datetime(2026, 9, 29, tzinfo=BUSINESS_TZ),
    )
    assert x.bid_due.value is None and x.bid_due.confidence == 0.0


def test_month_day_only_keeps_this_year_inside_the_grace_window():  # type: ignore[no-untyped-def]
    """ "10-06" sent on the 8th is a missed deadline, not next October (SPEC-02 F3)."""
    x = postprocess(
        _llm(bid_due=LLMDateTimeField(value="10-06", confidence=0.9)),
        datetime(2026, 10, 8, 9, 0, tzinfo=BUSINESS_TZ),
    )
    assert x.bid_due.value == datetime(2026, 10, 6, tzinfo=BUSINESS_TZ)
    assert Flag.past_due_at_receipt in x.flags and x.bid_due.confidence <= 0.3


def test_month_day_only_rolls_over_a_december_to_january_gap():  # type: ignore[no-untyped-def]
    x = postprocess(
        _llm(bid_due=LLMDateTimeField(value="01-08T14:00", time_known=True, confidence=0.95)),
        datetime(2026, 12, 21, 10, 5, tzinfo=BUSINESS_TZ),
    )
    assert x.bid_due.value == datetime(2027, 1, 8, 14, 0, tzinfo=BUSINESS_TZ)
    assert Flag.past_due_at_receipt not in x.flags


def test_midnight_the_model_did_not_claim_is_not_a_time():  # type: ignore[no-untyped-def]
    x = postprocess(
        _llm(
            bid_due=LLMDateTimeField(value="2026-10-16T00:00:00", time_known=False, confidence=0.8)
        ),
        datetime(2026, 9, 30, tzinfo=BUSINESS_TZ),
    )
    assert (
        x.bid_due.value == datetime(2026, 10, 16, tzinfo=BUSINESS_TZ) and not x.bid_due.time_known
    )


def test_foreign_timezone_is_recorded_on_the_field():  # type: ignore[no-untyped-def]
    x = postprocess(
        _llm(
            bid_due=LLMDateTimeField(
                value="2026-10-20T14:00:00",
                timezone="America/Chicago",
                time_known=True,
                confidence=0.9,
            )
        ),
        datetime(2026, 9, 29, tzinfo=BUSINESS_TZ),
    )
    assert x.bid_due.timezone == "America/Chicago"
    assert x.bid_due.value.astimezone(BUSINESS_TZ).hour == 15


def test_unknown_timezone_falls_back_to_business_tz():  # type: ignore[no-untyped-def]
    x = postprocess(
        _llm(
            bid_due=LLMDateTimeField(
                value="2026-10-20T14:00:00",
                timezone="Mars/Olympus",
                time_known=True,
                confidence=0.9,
            )
        ),
        datetime(2026, 9, 29, tzinfo=BUSINESS_TZ),
    )
    assert x.bid_due.value == datetime(2026, 10, 20, 14, 0, tzinfo=BUSINESS_TZ)


def test_source_excerpt_capped_at_200_chars():  # type: ignore[no-untyped-def]
    long_quote = "bids are due soon " * 30
    x = postprocess(
        _llm(project_name=StrField(value="A job", confidence=0.9, source=long_quote)),
        datetime(2026, 9, 30, tzinfo=BUSINESS_TZ),
    )
    assert len(x.project_name.source) == 200 and x.project_name.source.endswith("…")


def test_source_location_vocabulary_is_enforced():  # type: ignore[no-untyped-def]
    x = postprocess(
        _llm(
            project_name=StrField(value="A", confidence=0.9, source="A", source_location="SUBJECT"),
            gc_name=StrField(
                value="B", confidence=0.9, source="B", source_location="my best guess"
            ),
            owner_name=StrField(
                value="C", confidence=0.9, source="C", source_location="attachment:ITB.pdf:p3"
            ),
        ),
        datetime(2026, 9, 30, tzinfo=BUSINESS_TZ),
    )
    assert x.project_name.source_location == "subject"
    assert x.gc_name.source_location is None
    assert x.owner_name.source_location == "attachment:ITB.pdf:p3"


def test_platform_is_never_the_gc():  # type: ignore[no-untyped-def]
    x = postprocess(
        _llm(
            gc_name=StrField(
                value="BuildingConnected", confidence=0.8, source="from BuildingConnected"
            )
        ),
        datetime(2026, 9, 30, tzinfo=BUSINESS_TZ),
    )
    assert x.gc_name.value is None and x.gc_name.confidence == 0.0


def test_platform_contact_without_an_email_is_dropped_too():  # type: ignore[no-untyped-def]
    x = postprocess(
        _llm(gc_contacts=[Contact(name="Procore"), Contact(name="Jane", phone="412-555-0100")]),
        datetime(2026, 9, 30, tzinfo=BUSINESS_TZ),
    )
    assert [c.name for c in x.gc_contacts] == ["Jane"]


def test_location_starts_ungeocoded():  # type: ignore[no-untyped-def]
    from bidtriage.extraction.schema import GeoPrecision, Location

    x = postprocess(
        _llm(
            location=Location(
                raw="downtown Pittsburgh", city="Pittsburgh", state="PA", confidence=0.6
            )
        ),
        datetime(2026, 9, 30, tzinfo=BUSINESS_TZ),
    )
    assert x.location.geo is None and x.location.geo_precision == GeoPrecision.none
    assert x.location.raw == "downtown Pittsburgh"


def test_bare_month_day_has_no_time_and_modest_confidence():  # type: ignore[no-untyped-def]
    """A bare "bids due 10/16" and nothing more: the date is firm, the hour is not (SPEC-02)."""
    x = postprocess(
        _llm(bid_due=LLMDateTimeField(value="10/16", confidence=0.8)),
        datetime(2026, 9, 30, tzinfo=BUSINESS_TZ),
    )
    assert x.bid_due.value == datetime(2026, 10, 16, tzinfo=BUSINESS_TZ)
    assert not x.bid_due.time_known and x.bid_due.confidence <= 0.8


def test_source_location_vocabulary_is_closed_not_a_prefix_match():  # type: ignore[no-untyped-def]
    """A location has to be openable. "body of the email" is prose, not a place in the message."""
    from bidtriage.extraction.postprocess import location_label

    assert location_label("subject") == "subject"
    assert location_label("BODY") == "body"
    assert location_label("attachment:ITB.pdf") == "attachment:ITB.pdf"
    assert location_label("attachment:ITB.pdf:p3") == "attachment:ITB.pdf:p3"
    # Filenames may contain colons; only a trailing :p<digits> is a page.
    assert location_label("attachment:Report: Final.pdf:p12") == "attachment:Report: Final.pdf:p12"
    assert location_label("attachment:ITB.pdf:p03") == "attachment:ITB.pdf:p3"
    for junk in (
        "body of the email",
        "subjectively speaking",
        "the body",
        "attachment",
        "attachment:",
        "",
    ):
        assert location_label(junk) is None, junk
