from datetime import datetime

from bidtriage.core.clock import BUSINESS_TZ
from bidtriage.extraction.postprocess import parse_local, postprocess
from bidtriage.extraction.schema import Contact, Flag, LLMDateTimeField, LLMExtraction, LLMPrebid


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
