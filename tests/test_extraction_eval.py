"""The eval harness that guards the SPEC-02 accuracy goals, and the CLI gate over it."""

from __future__ import annotations

from datetime import datetime

from typer.testing import CliRunner

from bidtriage.cli import app, load_eval_cases
from bidtriage.core.clock import BUSINESS_TZ
from bidtriage.extraction.evaluate import (
    THRESHOLDS,
    EvalCase,
    compare,
    evaluate,
    format_report,
    size_band,
)
from bidtriage.extraction.postprocess import postprocess
from bidtriage.extraction.protocol import ExtractionInput
from bidtriage.extraction.schema import (
    ExtractedOpportunity,
    LLMDateTimeField,
    LLMExtraction,
    SizeSignals,
)

SENT = datetime(2026, 9, 30, 9, 0, tzinfo=BUSINESS_TZ)


def _llm(**kw):  # type: ignore[no-untyped-def]
    base = dict(kind="itb", kind_confidence=0.95, summary="A job.")
    base.update(kw)
    return LLMExtraction.model_validate(base)


def _case(name: str, llm: LLMExtraction) -> EvalCase:
    return EvalCase(
        name=name,
        item=ExtractionInput(
            message_id=name,
            subject="ITB",
            from_addr="jdoe@pjdick.com",
            from_name="Jane Doe",
            to=["estimating@ferryelectric.com"],
            sent_at=SENT,
            body="body",
        ),
        expected=llm,
    )


class Constant:
    def __init__(self, llm: LLMExtraction) -> None:
        self.llm = llm

    def extract(self, item: ExtractionInput) -> ExtractedOpportunity:
        return postprocess(self.llm, item.sent_at)


def test_size_band_separates_electrical_from_project_value():  # type: ignore[no-untyped-def]
    assert size_band(SizeSignals(stated_electrical_value=850_000)) == "elec:250K-1M"
    assert size_band(SizeSignals(stated_project_value=12_000_000)) == "proj:4M-15M"
    assert size_band(SizeSignals()) == "unknown"
    # An electrical number, when stated, wins: it is what the estimator actually bids.
    assert (
        size_band(SizeSignals(stated_project_value=12_000_000, stated_electrical_value=1_500_000))
        == "elec:1M-4M"
    )


def test_a_wrong_month_is_a_due_date_miss():  # type: ignore[no-untyped-def]
    expected = postprocess(
        _llm(
            bid_due=LLMDateTimeField(value="2026-10-16T14:00:00", time_known=True, confidence=0.9)
        ),
        SENT,
    )
    got = postprocess(
        _llm(
            bid_due=LLMDateTimeField(value="2026-11-16T14:00:00", time_known=True, confidence=0.9)
        ),
        SENT,
    )
    assert compare(expected, got)["bid_due"] is False


def test_the_same_day_at_the_wrong_hour_is_a_due_date_miss():  # type: ignore[no-untyped-def]
    expected = postprocess(
        _llm(
            bid_due=LLMDateTimeField(value="2026-10-16T14:00:00", time_known=True, confidence=0.9)
        ),
        SENT,
    )
    got = postprocess(
        _llm(
            bid_due=LLMDateTimeField(value="2026-10-16T10:00:00", time_known=True, confidence=0.9)
        ),
        SENT,
    )
    assert compare(expected, got)["bid_due"] is False


def test_a_date_only_expectation_ignores_the_hour():  # type: ignore[no-untyped-def]
    expected = postprocess(_llm(bid_due=LLMDateTimeField(value="2026-10-16", confidence=0.7)), SENT)
    got = postprocess(_llm(bid_due=LLMDateTimeField(value="2026-10-16", confidence=0.7)), SENT)
    assert compare(expected, got)["bid_due"] is True


def test_itb_boundary_is_scored_separately_from_the_exact_kind():  # type: ignore[no-untyped-def]
    expected = postprocess(_llm(kind="addendum"), SENT)
    got = postprocess(_llm(kind="date_change"), SENT)
    verdicts = compare(expected, got)
    # Both sides agree it is not a new invitation, which is the boundary SPEC-02 holds to 97%.
    assert verdicts["kind"] is False and verdicts["itb_boundary"] is True


def test_report_fails_the_gate_when_due_dates_slip():  # type: ignore[no-untyped-def]
    right = _llm(
        bid_due=LLMDateTimeField(value="2026-10-16T14:00:00", time_known=True, confidence=0.9)
    )
    wrong = _llm(
        bid_due=LLMDateTimeField(value="2026-12-01T14:00:00", time_known=True, confidence=0.9)
    )
    report = evaluate([_case("a", right)], Constant(wrong))
    failures = {f.name for f in report.gate_failures}
    assert "bid_due" in failures
    assert any("gate 97%: FAIL" in line for line in format_report(report))


def test_an_extractor_that_raises_is_reported_not_swallowed():  # type: ignore[no-untyped-def]
    class Boom:
        def extract(self, item):  # type: ignore[no-untyped-def]
            raise ConnectionError("anthropic unreachable")

    report = evaluate([_case("a", _llm())], Boom())
    assert report.cases == [] and report.errors[0][0] == "a"
    assert "ERROR ConnectionError" in "\n".join(format_report(report))


def test_thresholds_match_the_spec_goals():  # type: ignore[no-untyped-def]
    assert THRESHOLDS == {
        "bid_due": 0.97,
        "gc_name": 0.98,
        "project_type": 0.90,
        "size_band": 0.80,
        "itb_boundary": 0.97,
    }


def test_fixture_corpus_covers_the_spec_edge_cases(fixtures_dir):  # type: ignore[no-untyped-def]
    """Every case named in the SPEC-02 edge-case table has a fixture."""
    required = {
        "date_year_rollover",
        "date_relative_weekday",
        "date_conflict_gc_owner",
        "date_already_past",
        "date_foreign_timezone",
        "prebid_optional",
        "no_due_date",
        "size_project_value_only",
        "size_electrical_value",
        "size_sf_stories",
        "size_range",
        "scope_with_exclusion",
        "design_build",
        "procore_table",
        "forwarded_thread",
        "gc_from_domain",
        "cm_and_owner",
        "ocr_garbled",
        "huge_spec_book",
        "itb_vs_rfb",
        "cancelled",
        "vague_location",
        "wv_location",
        "typo_time",
        "empty_summary",
        "refusal_stub",
    }
    present = {p.stem for p in fixtures_dir.glob("*.eml")}
    assert required <= present, required - present


def test_every_fixture_loads_as_an_eval_case(fixtures_dir):  # type: ignore[no-untyped-def]
    cases = load_eval_cases(fixtures_dir)
    assert len(cases) >= 30
    assert all(c.item.sent_at is not None for c in cases)
    # The PDF-only invitation really does arrive with attachment text, or the case proves nothing.
    attachment_only = next(c for c in cases if c.name == "attachment_only.eml")
    assert attachment_only.item.body.strip() == ""
    assert "RYCON CONSTRUCTION" in attachment_only.item.attachments[0].text


def test_cli_eval_reports_and_exits_zero_offline(fixtures_dir):  # type: ignore[no-untyped-def]
    result = CliRunner().invoke(app, ["eval-extraction", str(fixtures_dir), "--offline"])
    assert result.exit_code == 0, result.output
    assert "gate 97%: PASS" in result.output


def test_cli_eval_rejects_an_empty_directory(tmp_path):  # type: ignore[no-untyped-def]
    result = CliRunner().invoke(app, ["eval-extraction", str(tmp_path), "--offline"])
    assert result.exit_code == 1 and "no fixtures" in result.output
